# -*- coding: utf-8 -*-
"""
Cliente XML-RPC para conexión a Odoo 19 (Odoo.sh).

Maneja autenticación, ejecución de búsquedas y lecturas de modelos
con paginación automática y manejo de errores.
"""
from __future__ import annotations

import http.client
import logging
import xmlrpc.client
from dataclasses import dataclass
from typing import Any, Iterable

from .secrets_loader import get_secret

logger = logging.getLogger(__name__)


class OdooConnectionError(Exception):
    """Error de conexión o autenticación con Odoo."""


@dataclass
class OdooCredentials:
    """Credenciales para conectarse a Odoo."""

    url: str
    db: str
    username: str
    api_key: str

    @classmethod
    def from_env(cls) -> "OdooCredentials":
        """
        Carga credenciales desde la fuente unificada (`get_secret`):
        - En Streamlit Cloud, leen de `st.secrets`.
        - En desarrollo local, leen del `.env` vía `python-dotenv`.
        """
        url = (get_secret("ODOO_URL") or "").rstrip("/")
        db = get_secret("ODOO_DB") or ""
        username = get_secret("ODOO_USERNAME") or ""
        api_key = get_secret("ODOO_API_KEY") or ""

        missing = [
            name
            for name, value in [
                ("ODOO_URL", url),
                ("ODOO_DB", db),
                ("ODOO_USERNAME", username),
                ("ODOO_API_KEY", api_key),
            ]
            if not value
        ]
        if missing:
            raise OdooConnectionError(
                f"Faltan credenciales: {', '.join(missing)}. "
                f"En Streamlit Cloud configúralas en Settings → Secrets. "
                f"En local, revisa tu archivo `.env`."
            )

        return cls(url=url, db=db, username=username, api_key=api_key)


class OdooClient:
    """
    Cliente para interactuar con Odoo vía XML-RPC.

    Uso típico:
        client = OdooClient.from_env()
        client.authenticate()
        invoices = client.search_read(
            "account.move",
            domain=[("move_type", "=", "out_invoice"), ("state", "=", "posted")],
            fields=["name", "partner_id", "amount_total"],
        )
    """

    BATCH_SIZE = 500  # Tamaño de página para evitar timeouts

    def __init__(self, credentials: OdooCredentials) -> None:
        self.credentials = credentials
        self._uid: int | None = None
        self._common: xmlrpc.client.ServerProxy | None = None
        self._models: xmlrpc.client.ServerProxy | None = None

    @classmethod
    def from_env(cls) -> "OdooClient":
        """Crea cliente desde variables de entorno."""
        return cls(OdooCredentials.from_env())

    @property
    def uid(self) -> int:
        """ID del usuario autenticado."""
        if self._uid is None:
            raise OdooConnectionError(
                "Cliente no autenticado. Llama a .authenticate() primero."
            )
        return self._uid

    def _build_common_proxy(self) -> xmlrpc.client.ServerProxy:
        """Crea un ServerProxy fresco para /xmlrpc/2/common."""
        return xmlrpc.client.ServerProxy(
            f"{self.credentials.url}/xmlrpc/2/common",
            allow_none=True,
        )

    def _build_models_proxy(self) -> xmlrpc.client.ServerProxy:
        """
        Crea un ServerProxy fresco para /xmlrpc/2/object.

        IMPORTANTE: NO cacheamos el proxy entre llamadas. xmlrpc.client.ServerProxy
        mantiene una conexión HTTP persistente que puede quedar en estado
        'request in progress' (CannotSendRequest) si una llamada anterior fue
        interrumpida (común con Streamlit cuando el usuario hace rerun).
        Crear un proxy nuevo por cada execute_kw evita ese problema.
        """
        return xmlrpc.client.ServerProxy(
            f"{self.credentials.url}/xmlrpc/2/object",
            allow_none=True,
        )

    def authenticate(self) -> int:
        """
        Autentica contra Odoo usando API key.

        Retorna el UID del usuario. Lanza OdooConnectionError si falla.
        """
        try:
            common = self._build_common_proxy()
            # Test de versión (valida que la URL responde)
            version_info = common.version()
            logger.info("Odoo versión detectada: %s", version_info.get("server_version"))

            uid = common.authenticate(
                self.credentials.db,
                self.credentials.username,
                self.credentials.api_key,
                {},
            )
            if not uid:
                raise OdooConnectionError(
                    "Autenticación fallida. Verifica usuario, base de datos y API key."
                )

            self._uid = uid
            self._common = common  # solo para referencia/debug, no se reusa
            # Marcamos como autenticado; los proxies de "object" se crean por llamada
            self._models = True  # type: ignore[assignment]
            logger.info("Autenticado como UID=%s en DB=%s", uid, self.credentials.db)
            return uid
        except xmlrpc.client.Fault as exc:
            raise OdooConnectionError(f"Error XML-RPC: {exc.faultString}") from exc
        except Exception as exc:
            raise OdooConnectionError(
                f"No se pudo conectar a {self.credentials.url}: {exc}"
            ) from exc

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """
        Ejecuta un método arbitrario sobre un modelo.

        Crea un ServerProxy fresco en cada llamada para evitar el bug de
        CannotSendRequest cuando el proxy queda en estado inconsistente.
        Reintenta hasta 2 veces con un proxy nuevo si la conexión falla.

        Si se pasa `context`, se inyecta dentro de kwargs como `context=...`,
        que es la convención que entiende el ORM de Odoo. Esto es necesario
        para resolver campos `company_dependent` (credit_limit, dso, etc.)
        contra una empresa específica vía `allowed_company_ids`.
        """
        if self._models is None:
            self.authenticate()

        merged_kwargs: dict[str, Any] = dict(kwargs) if kwargs else {}
        if context:
            existing_ctx = merged_kwargs.get("context") or {}
            merged_kwargs["context"] = {**existing_ctx, **context}

        last_exc: Exception | None = None
        for attempt in range(2):
            proxy = self._build_models_proxy()
            try:
                return proxy.execute_kw(
                    self.credentials.db,
                    self.uid,
                    self.credentials.api_key,
                    model,
                    method,
                    args or [],
                    merged_kwargs,
                )
            except xmlrpc.client.Fault as exc:
                # Errores de Odoo (campo inválido, permisos, etc.) NO se reintentan
                raise OdooConnectionError(
                    f"Error ejecutando {model}.{method}: {exc.faultString}"
                ) from exc
            except (http.client.CannotSendRequest, http.client.HTTPException, OSError) as exc:
                # Errores de transporte: descartamos el proxy y reintentamos
                last_exc = exc
                logger.warning(
                    "Reintentando %s.%s tras error de transporte (intento %s): %s",
                    model, method, attempt + 1, exc,
                )
                continue
        raise OdooConnectionError(
            f"Error de transporte XML-RPC ejecutando {model}.{method}: {last_exc}"
        )

    def search(
        self,
        model: str,
        domain: list | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[int]:
        """Busca IDs que cumplen el dominio."""
        kwargs: dict[str, Any] = {"offset": offset}
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute_kw(model, "search", [domain or []], kwargs, context=context)

    def read(
        self,
        model: str,
        ids: Iterable[int],
        fields: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Lee registros por ID."""
        return self.execute_kw(
            model,
            "read",
            [list(ids)],
            {"fields": fields} if fields else {},
            context=context,
        )

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        order: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        """
        Búsqueda + lectura en un solo round-trip, con paginación automática.

        Si no se pasa limit, descarga todos los registros en lotes de BATCH_SIZE.
        """
        domain = domain or []
        kwargs: dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        if order:
            kwargs["order"] = order

        if limit is not None:
            kwargs["limit"] = limit
            return self.execute_kw(model, "search_read", [domain], kwargs, context=context)

        # Paginación automática
        results: list[dict] = []
        offset = 0
        while True:
            page_kwargs = dict(kwargs)
            page_kwargs["limit"] = self.BATCH_SIZE
            page_kwargs["offset"] = offset
            page = self.execute_kw(
                model, "search_read", [domain], page_kwargs, context=context
            )
            if not page:
                break
            results.extend(page)
            if len(page) < self.BATCH_SIZE:
                break
            offset += self.BATCH_SIZE
        return results

    def search_count(
        self,
        model: str,
        domain: list | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        """Cuenta registros que cumplen el dominio."""
        return self.execute_kw(model, "search_count", [domain or []], context=context)

    def fields_get(
        self,
        model: str,
        attributes: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, dict]:
        """Devuelve la definición de campos del modelo (útil para debug)."""
        return self.execute_kw(
            model,
            "fields_get",
            [],
            {"attributes": attributes or ["string", "type", "required"]},
            context=context,
        )

    def test_connection(self) -> dict:
        """
        Test rápido de la conexión. Devuelve diagnóstico para mostrar al usuario.
        """
        info: dict[str, Any] = {
            "url": self.credentials.url,
            "db": self.credentials.db,
            "username": self.credentials.username,
        }
        try:
            uid = self.authenticate()
            info["status"] = "ok"
            info["uid"] = uid
            # Cuenta clientes y facturas para validar permisos
            info["partners_count"] = self.search_count(
                "res.partner", [("customer_rank", ">", 0)]
            )
            info["invoices_count"] = self.search_count(
                "account.move",
                [("move_type", "=", "out_invoice"), ("state", "=", "posted")],
            )
        except OdooConnectionError as exc:
            info["status"] = "error"
            info["error"] = str(exc)
        return info
