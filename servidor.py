"""
AlphaDesk — Servidor
FastAPI servindo os HTMLs estáticos + rotas /api/* para salvar JSONs.
Deploy no Render.com (plano free, sem disco externo).
"""

import os
import json
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Any, List

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ────────────────────────────────────────────────────────────────────
def ler_json(nome: str, default=None):
    path = DATA_DIR / nome
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def salvar_json(nome: str, data: Any):
    path = DATA_DIR / nome
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── FastAPI ────────────────────────────────────────────────────────────────────
api = FastAPI(title="AlphaDesk API", version="1.0.0")

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── Schemas ────────────────────────────────────────────────────────────────────
class RecomendadosPayload(BaseModel):
    recomendados: list
    aprovados: list = []

class PesosPayload(BaseModel):
    senha: str = ""
    pesos: List[float]  # FIX: lista, não dict

class GestorasPayload(BaseModel):
    gestoras: dict

# ── API ────────────────────────────────────────────────────────────────────────
@api.get("/api/load-estado")
def load_estado():
    estado = ler_json("estado.json", {})
    # Também tenta carregar pesos do pesos.json legado se estado não tiver
    if "pesos" not in estado:
        pesos_legado = ler_json("pesos.json", None)
        if isinstance(pesos_legado, list):
            estado["pesos"] = pesos_legado
    return JSONResponse(content=estado)

@api.post("/api/save-recomendados")
def save_recomendados(payload: RecomendadosPayload):
    dados = ler_json("recomendados.json", {"recomendados": [], "aprovados": []})
    dados["recomendados"] = payload.recomendados
    dados["aprovados"]    = payload.aprovados
    salvar_json("recomendados.json", dados)
    return {"ok": True, "total": len(payload.recomendados)}

@api.post("/api/save-pesos")
def save_pesos(payload: PesosPayload):
    estado = ler_json("estado.json", {})
    estado["pesos"] = payload.pesos  # lista agora
    estado["pesos_atualizados"] = datetime.now().isoformat()
    salvar_json("estado.json", estado)
    # Também salva pesos.json para compatibilidade
    salvar_json("pesos.json", payload.pesos)
    return {"ok": True}

@api.post("/api/save-gestoras")
def save_gestoras(payload: GestorasPayload):
    salvar_json("gestoras.json", payload.gestoras)
    return {"ok": True, "total": len(payload.gestoras)}

# ── Servir JSONs ───────────────────────────────────────────────────────────────
@api.get("/data/{filename}")
def serve_data(filename: str):
    allowed = {
        "fundos.json", "benchmarks.json", "cotas.json",
        "meta.json", "gestoras.json", "recomendados.json", "conteudo.json",
        "estado.json", "pesos.json"
    }
    if filename not in allowed:
        raise HTTPException(404, "Arquivo não encontrado")
    path = DATA_DIR / filename
    if not path.exists():
        raise HTTPException(404, f"{filename} não encontrado")
    return FileResponse(str(path), media_type="application/json")

# ── Servir HTMLs principais ────────────────────────────────────────────────────
@api.get("/", include_in_schema=False)
def root():
    return FileResponse(str(BASE_DIR / "index.html"))

@api.get("/dashboard.html", include_in_schema=False)
def dashboard():
    return FileResponse(str(BASE_DIR / "dashboard.html"))

# ── Comparador ─────────────────────────────────────────────────────────────────
@api.get("/comparador", include_in_schema=False)
@api.get("/comparador/", include_in_schema=False)
@api.get("/comparador/carteiras.html", include_in_schema=False)
def comparador_html():
    path = BASE_DIR / "comparador" / "carteiras.html"
    if not path.exists():
        raise HTTPException(404, f"carteiras.html não encontrado em {path}")
    return FileResponse(str(path), media_type="text/html")

comparador_dir = BASE_DIR / "comparador"
if comparador_dir.exists():
    api.mount("/comparador", StaticFiles(directory=str(comparador_dir)), name="comparador")

# ── Startup ────────────────────────────────────────────────────────────────────
@api.on_event("startup")
def startup():
    print(f"✅ AlphaDesk iniciado")
    print(f"   BASE_DIR: {BASE_DIR}")
    print(f"   DATA_DIR: {DATA_DIR}")
    estado = ler_json("estado.json", {})
    pesos = estado.get("pesos", ler_json("pesos.json", []))
    print(f"   pesos carregados: {pesos}")
    comp = BASE_DIR / "comparador"
    print(f"   comparador/: {'✓ existe' if comp.exists() else '✗ NÃO ENCONTRADO'}")
    if comp.exists():
        print(f"   arquivos: {[f.name for f in comp.iterdir()]}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("servidor:api", host="0.0.0.0", port=8000, reload=False)
