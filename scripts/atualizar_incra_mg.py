import os
import json
import time
import shutil
import tempfile
import zipfile
import urllib.parse
import urllib.request
import geopandas as gpd
import pandas as pd

# Bounding Box aproximado de Minas Gerais (EPSG:4674 - SIRGAS 2000)
BBOX_MG = (-51.05, -22.93, -39.85, -14.23)

WFS_BASE_URL = "https://geonodeincra.serpro.gov.br/geoserver/ows"
TYPENAME = "geonode:sigef_geo"

URL_SHAPE_ZIP_COMPLETO = (
    "https://geonodeincra.serpro.gov.br/geoserver/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typename=geonode%3Asigef_geo&outputFormat=SHAPE-ZIP"
    "&srs=EPSG%3A4674&format_options=charset%3AUTF-8"
)

PASTA_SAIDA = "dist_incra"
CAMINHO_GPKG = os.path.join(PASTA_SAIDA, "incra_mg.gpkg")
CAMINHO_ZIP  = os.path.join(PASTA_SAIDA, "incra_mg.zip")


def baixar_wfs_paginado_mg(tamanho_pagina=2000):
    """Consulta o GeoServer SERPRO via WFS 2.0.0 filtrado pelo BBOX de Minas Gerais."""
    gdfs = []
    start_index = 0
    xmin, ymin, xmax, ymax = BBOX_MG

    while True:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": TYPENAME,
            "outputFormat": "application/json",
            "srsName": "EPSG:4674",
            "bbox": f"{ymin},{xmin},{ymax},{xmax},urn:ogc:def:crs:EPSG::4674",
            "count": tamanho_pagina,
            "startIndex": start_index,
        }
        url_req = f"{WFS_BASE_URL}?{urllib.parse.urlencode(params)}"
        print(f"[WFS-MG] Baixando lote a partir do índice {start_index}...")

        try:
            req = urllib.request.Request(url_req, headers={"User-Agent": "QGIS-Plugin-APPs-MG/1.1"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                conteudo = resp.read().decode("utf-8", errors="ignore")

            dados = json.loads(conteudo)
            feicoes = dados.get("features", [])
            if not feicoes:
                break

            gdf_lote = gpd.GeoDataFrame.from_features(feicoes, crs="EPSG:4674")
            gdfs.append(gdf_lote)
            print(f"[WFS-MG] +{len(gdf_lote)} feições recebidas (acumulado: {sum(len(g) for g in gdfs)}).")

            if len(feicoes) < tamanho_pagina:
                break

            start_index += tamanho_pagina
            time.sleep(1.0)

        except Exception as e:
            print(f"[WFS-MG] Interrupção no índice {start_index}: {e}")
            break

    if not gdfs:
        return None

    return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs="EPSG:4674")


def baixar_via_shape_zip():
    """Fallback: baixa o SHAPE-ZIP oficial completo e recorta espacialmente pelo BBOX de MG."""
    print("[FALLBACK] Baixando SHAPE-ZIP completo do GeoServer SERPRO...")
    tmp_dir = tempfile.mkdtemp(prefix="incra_zip_")
    zip_local = os.path.join(tmp_dir, "sigef_geo.zip")

    try:
        req = urllib.request.Request(URL_SHAPE_ZIP_COMPLETO, headers={"User-Agent": "QGIS-Plugin-APPs-MG/1.1"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(zip_local, "wb") as f_out:
            shutil.copyfileobj(resp, f_out)

        with zipfile.ZipFile(zip_local, "r") as zf:
            zf.extractall(tmp_dir)

        shps = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.lower().endswith(".shp")]
        if not shps:
            return None

        print("[FALLBACK] Lendo shapefile filtrado pelo retângulo de Minas Gerais...")
        gdf = gpd.read_file(shps[0], bbox=BBOX_MG)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4674")
        elif gdf.crs.to_epsg() != 4674:
            gdf = gdf.to_crs("EPSG:4674")
        return gdf

    except Exception as e:
        print(f"[FALLBACK] Falha ao baixar SHAPE-ZIP: {e}")
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def limpar_e_padronizar(gdf):
    """Filtra linhas com codigo_imo nulo, valida geometrias e mantém colunas essenciais."""
    if gdf is None or gdf.empty:
        return None

    gdf.columns = [c.lower() for c in gdf.columns]

    if "codigo_imo" not in gdf.columns:
        print(f"Colunas disponíveis na camada: {list(gdf.columns)}")
        return None

    gdf["codigo_imo"] = gdf["codigo_imo"].astype(str).str.strip()
    gdf = gdf[
        gdf["codigo_imo"].notna()
        & (gdf["codigo_imo"] != "")
        & (~gdf["codigo_imo"].str.lower().isin(["none", "nan", "null"]))
    ].copy()

    if gdf.empty:
        return None

    # Filtra municípios de MG (código IBGE iniciado em 31), mantendo linhas onde muniorig1 está nulo
    if "muniorig1" in gdf.columns:
        mun = gdf["muniorig1"].astype(str).str.strip()
        mascara_mg = mun.str.startswith("31") | mun.str.lower().isin(["", "none", "nan", "null"])
        gdf = gdf[mascara_mg].copy()
    else:
        gdf["muniorig1"] = ""

    for col in ["nome_area", "area_ha"]:
        if col not in gdf.columns:
            gdf[col] = ""

    gdf = gdf[["codigo_imo", "muniorig1", "nome_area", "area_ha", "geometry"]].copy()

    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
    gdf["geometry"] = gdf.geometry.buffer(0)
    gdf = gdf.cx[BBOX_MG[0]:BBOX_MG[2], BBOX_MG[1]:BBOX_MG[3]]
    gdf = gdf.drop_duplicates(subset=["codigo_imo"], keep="first")

    return gdf


def main():
    os.makedirs(PASTA_SAIDA, exist_ok=True)

    gdf_bruto = baixar_wfs_paginado_mg()
    if gdf_bruto is None or gdf_bruto.empty:
        gdf_bruto = baixar_via_shape_zip()

    gdf_final = limpar_e_padronizar(gdf_bruto)
    if gdf_final is None or gdf_final.empty:
        raise RuntimeError(
            "Nenhuma feição válida de MG foi obtida do servidor GeoNode INCRA/SERPRO. "
            "A Release anterior será preservada."
        )

    print(f"Salvando GeoPackage consolidado com {len(gdf_final)} imóveis em {CAMINHO_GPKG}...")
    gdf_final.to_file(CAMINHO_GPKG, layer="imoveis_incra_mg", driver="GPKG")

    print(f"Compactando para {CAMINHO_ZIP}...")
    with zipfile.ZipFile(CAMINHO_ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(CAMINHO_GPKG, arcname="incra_mg.gpkg")

    tamanho_mb = os.path.getsize(CAMINHO_ZIP) / (1024 * 1024)
    print(f"Concluído! Arquivo {CAMINHO_ZIP} gerado com {tamanho_mb:.2f} MB.")


if __name__ == "__main__":
    main()
