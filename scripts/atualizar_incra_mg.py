import os
import json
import time
import zipfile
import urllib.parse
import urllib.request
import geopandas as gpd
import pandas as pd

# Bounding Box aproximado de Minas Gerais (EPSG:4674 - SIRGAS 2000)
BBOX_MG = (-51.05, -22.93, -39.85, -14.23)

# Endpoints WFS do Acervo Fundiário do INCRA (SIGEF Particular e SNCI)
FONTES_WFS = [
    {
        "nome": "sigef_particular",
        "url": "https://acervofundiario.incra.gov.br/i3geo/ogc.php?tema=certificada_sigef_particular_mg",
        "typename": "certificada_sigef_particular_mg"
    },
    {
        "nome": "snci_privado",
        "url": "https://acervofundiario.incra.gov.br/i3geo/ogc.php?tema=imoveis_privados_mg",
        "typename": "imoveis_privados_mg"
    }
]

PASTA_SAIDA = "dist_incra"
CAMINHO_GPKG = os.path.join(PASTA_SAIDA, "incra_mg.gpkg")
CAMINHO_ZIP  = os.path.join(PASTA_SAIDA, "incra_mg.zip")


def baixar_camada_wfs(fonte, tamanho_pagina=2000):
    """Baixa feições WFS paginadas em formato GeoJSON/GML e retorna um GeoDataFrame."""
    gdfs = []
    start_index = 0
    xmin, ymin, xmax, ymax = BBOX_MG

    while True:
        params = {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": fonte["typename"],
            "outputFormat": "application/json",
            "srsName": "EPSG:4674",
            "bbox": f"{xmin},{ymin},{xmax},{ymax},EPSG:4674",
            "maxFeatures": tamanho_pagina,
            "startIndex": start_index,
        }
        url_req = f"{fonte['url']}&{urllib.parse.urlencode(params)}"
        print(f"[{fonte['nome']}] Baixando lote a partir do índice {start_index}...")

        try:
            req = urllib.request.Request(url_req, headers={"User-Agent": "QGIS-Plugin-APPs-MG/1.1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                conteudo = resp.read().decode("utf-8", errors="ignore")

            dados = json.loads(conteudo)
            feicoes = dados.get("features", [])
            if not feicoes:
                break

            gdf_lote = gpd.GeoDataFrame.from_features(feicoes, crs="EPSG:4674")
            gdfs.append(gdf_lote)
            print(f"[{fonte['nome']}] +{len(gdf_lote)} feições recebidas.")

            if len(feicoes) < tamanho_pagina:
                break

            start_index += tamanho_pagina
            time.sleep(1.5)  # Pausa cordial para não sobrecarregar o servidor do INCRA

        except Exception as e:
            print(f"[{fonte['nome']}] Aviso/interrupção no índice {start_index}: {e}")
            break

    if not gdfs:
        return None

    return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs="EPSG:4674")


def limpar_e_padronizar(gdf, origem):
    """Filtra linhas com codigo_imo nulo, valida geometrias e mantém colunas essenciais."""
    if gdf is None or gdf.empty:
        return None

    # Normaliza nomes das colunas para minúsculo
    gdf.columns = [c.lower() for c in gdf.columns]

    # Garante existência de codigo_imo e remove registros nulos ou vazios
    if "codigo_imo" not in gdf.columns:
        return None

    gdf["codigo_imo"] = gdf["codigo_imo"].astype(str).str.strip()
    gdf = gdf[
        gdf["codigo_imo"].notna()
        & (gdf["codigo_imo"] != "")
        & (gdf["codigo_imo"].str.lower() != "none")
        & (gdf["codigo_imo"].str.lower() != "nan")
    ].copy()

    if gdf.empty:
        return None

    # Filtra registros cujo município (muniorig1) pertence a MG (código IBGE iniciado em 31), quando preenchido
    if "muniorig1" in gdf.columns:
        mun = gdf["muniorig1"].astype(str).str.strip()
        mascara_mg = mun.str.startswith("31") | mun.isin(["", "None", "nan", "NULL"])
        gdf = gdf[mascara_mg].copy()
    else:
        gdf["muniorig1"] = ""

    # Mantém apenas colunas essenciais para reduzir tamanho do GeoPackage
    colunas_desejadas = ["codigo_imo", "muniorig1", "nome_area", "area_ha", "geometry"]
    for col in colunas_desejadas:
        if col not in gdf.columns and col != "geometry":
            gdf[col] = ""

    gdf["fonte"] = origem
    gdf = gdf[["codigo_imo", "muniorig1", "nome_area", "area_ha", "fonte", "geometry"]].copy()

    # Corrige eventuais geometrias inválidas e filtra pelo retângulo de MG
    gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
    gdf["geometry"] = gdf.geometry.buffer(0)
    gdf = gdf.cx[BBOX_MG[0]:BBOX_MG[2], BBOX_MG[1]:BBOX_MG[3]]

    return gdf


def main():
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    camadas_consolidadas = []

    for fonte in FONTES_WFS:
        gdf_bruto = baixar_camada_wfs(fonte)
        gdf_limpo = limpar_e_padronizar(gdf_bruto, fonte["nome"])
        if gdf_limpo is not None and not gdf_limpo.empty:
            print(f"[{fonte['nome']}] Total após limpeza: {len(gdf_limpo)} imóveis válidos.")
            camadas_consolidadas.append(gdf_limpo)

    if not camadas_consolidadas:
        raise RuntimeError(
            "Nenhuma feição válida foi obtida do servidor WFS do INCRA nesta execução. "
            "A Release anterior será preservada."
        )

    gdf_final = gpd.GeoDataFrame(
        pd.concat(camadas_consolidadas, ignore_index=True),
        crs="EPSG:4674"
    )
    # Remove duplicidades exatas de codigo_imo mantendo a mais recente
    gdf_final = gdf_final.drop_duplicates(subset=["codigo_imo"], keep="first")

    print(f"Salvando GeoPackage consolidado com {len(gdf_final)} imóveis em {CAMINHO_GPKG}...")
    gdf_final.to_file(CAMINHO_GPKG, layer="imoveis_incra_mg", driver="GPKG")

    print(f"Compactando para {CAMINHO_ZIP}...")
    with zipfile.ZipFile(CAMINHO_ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(CAMINHO_GPKG, arcname="incra_mg.gpkg")

    tamanho_mb = os.path.getsize(CAMINHO_ZIP) / (1024 * 1024)
    print(f"Concluído! Arquivo {CAMINHO_ZIP} gerado com {tamanho_mb:.2f} MB.")


if __name__ == "__main__":
    main()