"""Exercise the real embedding save route against an isolated DB (no provider)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ai.embedding_config_service import EmbeddingConfigService
from infrastructure.persistence.database.connection import DatabaseConnection
from interfaces.api.v1.core.settings import embedding_router


def test_embedding_save_returns_saved_config_instead_of_500(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / 'embedding-config.db'))
    service = EmbeddingConfigService(db)
    monkeypatch.setattr('application.ai.embedding_config_service.get_embedding_config_service', lambda: service)
    app = FastAPI()
    app.include_router(embedding_router)
    try:
        with TestClient(app) as client:
            response = client.put('/settings/embedding/', json={'mode': 'openai', 'api_key': 'fixture-key-not-real',
                'base_url': 'https://api.siliconflow.cn/v1', 'model': 'Qwen/Qwen3-Embedding-4B'})
            assert response.status_code == 200
            assert response.json()['model'] == 'Qwen/Qwen3-Embedding-4B'
            assert client.get('/settings/embedding/').json()['api_key'] == 'fixture-key-not-real'
    finally:
        db.close_all()
