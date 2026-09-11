from pathlib import Path
import json, os, urllib.request
from dotenv import load_dotenv

DEFAULT = {
    "provider": "gemini",
    "model": "gemini-3.6-flash",
    "endpoint": "",
    "api_key_env": "GEMINI_API_KEY",
    "timeout_seconds": 90,
}

class _Result:
    def __init__(self, text): self.output_text = str(text or "")

class _Interactions:
    def __init__(self, owner): self.owner = owner
    def create(self, model=None, input=None, **kwargs):
        return _Result(self.owner.generate(str(input or ""), model=model, **kwargs))

class PrimaryLLM:
    """Small provider adapter used by every cloud/full-capability LLM feature.

    Secrets are never stored in llm.json. api_key_env names an environment variable
    in .env (or the process environment). Supported providers:
      - gemini: Google GenAI SDK
      - openai_compatible: /v1/chat/completions API (OpenAI, Groq, compatible gateways, etc.)
      - ollama: local /api/generate
    """
    def __init__(self, path, base_dir=None):
        self.path = Path(path)
        self.base_dir = Path(base_dir or self.path.parent.parent)
        # Load local secrets without ever persisting them in provider settings.
        load_dotenv(self.base_dir / ".env", override=False)
        self.config = self._read()
        self.provider = self.config["provider"]
        self.model = self.config["model"]
        self.interactions = _Interactions(self)  # compatibility with existing call sites

    def _read(self):
        x = dict(DEFAULT)
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict): x.update({k: raw[k] for k in DEFAULT if k in raw})
        except Exception:
            pass
        x["provider"] = str(x.get("provider") or "gemini").strip().lower()
        if x["provider"] not in ("gemini", "openai_compatible", "ollama"):
            x["provider"] = "gemini"
        x["model"] = str(x.get("model") or DEFAULT["model"]).strip()
        x["endpoint"] = str(x.get("endpoint") or "").strip().rstrip("/")
        x["api_key_env"] = str(x.get("api_key_env") or "").strip()
        x["timeout_seconds"] = max(5, min(600, int(x.get("timeout_seconds") or 90)))
        return x

    def public_config(self):
        return dict(self.config, secret_stored=False)

    def save_config(self, data):
        x = dict(self.config)
        for k in DEFAULT:
            if k in data: x[k] = data[k]
        provider = str(x.get("provider") or "").strip().lower()
        if provider not in ("gemini", "openai_compatible", "ollama"):
            raise ValueError("provider must be gemini, openai_compatible, or ollama")
        x["provider"] = provider
        x["model"] = str(x.get("model") or "").strip()
        if not x["model"]: raise ValueError("model is required")
        x["endpoint"] = str(x.get("endpoint") or "").strip().rstrip("/")
        x["api_key_env"] = str(x.get("api_key_env") or "").strip()
        x["timeout_seconds"] = max(5, min(600, int(x.get("timeout_seconds") or 90)))
        # Store only provider metadata. Never write an API key/token to disk here.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(x, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config = x; self.provider = provider; self.model = x["model"]
        return self.public_config()

    def _key(self):
        name = self.config.get("api_key_env", "")
        return os.getenv(name, "") if name else ""

    def _json_request(self, url, payload, headers=None, timeout=None):
        data = json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "application/json"}; h.update(headers or {})
        req = urllib.request.Request(url, data=data, method="POST", headers=h)
        with urllib.request.urlopen(req, timeout=timeout or self.config["timeout_seconds"]) as r:
            return json.loads(r.read().decode("utf-8"))

    def generate(self, prompt, model=None, timeout=None, **_):
        provider = self.config["provider"]; model = model or self.config["model"]
        timeout = timeout or self.config["timeout_seconds"]
        if provider == "gemini":
            key = self._key()
            if not key: raise RuntimeError(f"Missing API key environment variable: {self.config.get('api_key_env') or 'GEMINI_API_KEY'}")
            from google import genai
            # google-genai expects HTTP timeout in milliseconds.
            client = genai.Client(api_key=key, http_options={"timeout": int(float(timeout) * 1000)})
            try:
                return (client.interactions.create(model=model, input=prompt).output_text or "").strip()
            finally:
                try: client.close()
                except Exception: pass
        if provider == "ollama":
            endpoint = self.config["endpoint"] or "http://127.0.0.1:11434"
            x = self._json_request(endpoint + "/api/generate", {"model": model, "prompt": prompt, "stream": False}, timeout=timeout)
            return str(x.get("response") or "").strip()
        endpoint = self.config["endpoint"] or "https://api.openai.com"
        if endpoint.endswith("/v1"): endpoint = endpoint[:-3]
        key = self._key()
        headers = {"Authorization": "Bearer " + key} if key else {}
        x = self._json_request(endpoint + "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": prompt}]}, headers, timeout)
        return str(x["choices"][0]["message"]["content"] or "").strip()

    def health(self, probe=False):
        try:
            if self.config["provider"] != "ollama" and self.config.get("api_key_env") and not self._key():
                return {"ok": False, "provider": self.provider, "model": self.model, "error": "Missing environment variable " + self.config["api_key_env"]}
            if not probe:
                return {"ok": True, "provider": self.provider, "model": self.model, "configured": True}
            text = self.generate("Return exactly: OK", timeout=min(30, self.config["timeout_seconds"]))
            return {"ok": bool(text), "provider": self.provider, "model": self.model, "probe_response": text[:120]}
        except Exception as e:
            return {"ok": False, "provider": self.provider, "model": self.model, "error": f"{type(e).__name__}: {e}"}
