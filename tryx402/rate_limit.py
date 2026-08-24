"""
Rate limiter in-memory par IP et clé, zero-dependency.
Conçu pour les architectures multi-tenant et les API agents.
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict
from typing import Optional, Callable
from fastapi import Request, HTTPException


class InMemoryRateLimiter:
    def __init__(self):
        # key -> list of timestamps (float)
        self._hits = defaultdict(list)
        self._lock = threading.Lock()

    def _clean_old_hits(self, key: str, window_seconds: float, now: float) -> list[float]:
        threshold = now - window_seconds
        valid_hits = [t for t in self._hits[key] if t > threshold]
        self._hits[key] = valid_hits
        return valid_hits

    def check(self, key: str, max_requests: int, window_seconds: float) -> bool:
        """
        Vérifie et incrémente le compteur pour une clé donnée dans une fenêtre glissante.
        Retourne True si autorisé, False si limite dépassée.
        """
        now = time.time()
        with self._lock:
            valid_hits = self._clean_old_hits(key, window_seconds, now)
            if len(valid_hits) >= max_requests:
                return False
            self._hits[key].append(now)
            return True

    def reset(self):
        """Réinitialise les compteurs (utile pour les tests)."""
        with self._lock:
            self._hits.clear()


limiter = InMemoryRateLimiter()


def get_client_ip(request: Request) -> str:
    """Extrait l'adresse IP du client en prenant en compte les proxys (Fly.io / Cloudflare)."""
    # Fly-Client-IP ou CF-Connecting-IP
    fly_ip = request.headers.get("fly-client-ip")
    if fly_ip:
        return fly_ip
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    
    # X-Forwarded-For (première IP de la chaîne)
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()

    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def rate_limit(
    max_requests: int,
    window_seconds: float,
    key_func: Optional[Callable[[Request], str]] = None,
    error_msg: str = "Trop de requêtes. Veuillez réessayer plus tard.",
):
    """
    Décorateur / dépendance FastAPI pour appliquer une limitation de débit.
    Par défaut, s'applique par IP de client.
    """
    def dependency(request: Request):
        ident = key_func(request) if key_func else get_client_ip(request)
        key = f"{request.url.path}:{ident}"
        
        if not limiter.check(key, max_requests, window_seconds):
            raise HTTPException(
                status_code=429,
                detail={"error": "rate_limit_exceeded", "message": error_msg},
                headers={"Retry-After": str(int(window_seconds))},
            )

    return dependency
