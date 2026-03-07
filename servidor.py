"""
AlphaDesk — Servidor
FastAPI servindo os HTMLs estáticos + rotas /api/* para salvar JSONs no disco persistente.
Deploy no Render.com com disco montado em /data.
"""

import os
import json
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Any

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# No Render: DATA_DIR = /data (disco persistente)
# Local:     DATA_DIR = ./data
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
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
    pesos: dict

class GestorasPayload(BaseModel):
    gestoras: dict

# ── API: leitura de estado ─────────────────────────────────────────────────────
@api.get("/api/load-estado")
def load_estado():
    estado = ler_json("estado.json", {})
    return JSONResponse(content=estado)

# ── API: salvar recomendados ───────────────────────────────────────────────────
@api.post("/api/save-recomendados")
def save_recomendados(payload: RecomendadosPayload):
    dados = ler_json("recomendados.json", {"recomendados": [], "aprovados": []})
    dados["recomendados"] = payload.recomendados
    dados["aprovados"]    = payload.aprovados
    salvar_json("recomendados.json", dados)
    return {"ok": True, "total": len(payload.recomendados)}

# ── API: salvar pesos ──────────────────────────────────────────────────────────
@api.post("/api/save-pesos")
def save_pesos(payload: PesosPayload):
    estado = ler_json("estado.json", {})
    estado["pesos"] = payload.pesos
    estado["pesos_atualizados"] = datetime.now().isoformat()
    salvar_json("estado.json", estado)
    return {"ok": True}

# ── API: salvar gestoras ───────────────────────────────────────────────────────
@api.post("/api/save-gestoras")
def save_gestoras(payload: GestorasPayload):
    salvar_json("gestoras.json", payload.gestoras)
    return {"ok": True, "total": len(payload.gestoras)}

# ── Servir JSONs da pasta /data ────────────────────────────────────────────────
@api.get("/data/{filename}")
def serve_data(filename: str):
    allowed = {
        "fundos.json", "benchmarks.json", "cotas.json",
        "meta.json", "gestoras.json", "recomendados.json", "conteudo.json"
    }
    if filename not in allowed:
        raise HTTPException(404, "Arquivo não encontrado")
    path = DATA_DIR / filename
    if not path.exists():
        raise HTTPException(404, f"{filename} não encontrado")
    return FileResponse(path, media_type="application/json")

# ── Servir HTMLs ───────────────────────────────────────────────────────────────
@api.get("/", include_in_schema=False)
def root():
    return FileResponse(BASE_DIR / "index.html")

@api.get("/dashboard.html", include_in_schema=False)
def dashboard():
    return FileResponse(BASE_DIR / "dashboard.html")

@api.get("/comparador/carteiras.html", include_in_schema=False)
def comparador():
    return FileResponse(BASE_DIR / "comparador" / "carteiras.html")

comparador_dir = BASE_DIR / "comparador"
if comparador_dir.exists():
    api.mount("/comparador", StaticFiles(directory=str(comparador_dir), html=True), name="comparador")

# ── Startup ────────────────────────────────────────────────────────────────────
@api.on_event("startup")
def startup():
    print(f"✅ AlphaDesk iniciado — dados em: {DATA_DIR}")
    jsons = list(DATA_DIR.glob("*.json"))
    print(f"   {len(jsons)} JSONs encontrados: {[j.name for j in jsons]}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("servidor:api", host="0.0.0.0", port=8000, reload=False)
