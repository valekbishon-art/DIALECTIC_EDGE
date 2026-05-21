"""
ai_provider.py — Мультипровайдер с роутингом по агентам.

Переменные окружения (Railway / .env):
  AI_DEBATE_PRIMARY — кто первым отвечает в дебатах:
      cerebras | mistral | groq | openrouter | together | gemini | mixed
  CEREBRAS_API_KEY — бесплатен! Получи на https://www.cerebras.ai/
  GROQ_MODEL, OPENROUTER_MODEL, TOGETHER_MODEL — модели для соответствующего primary
  MISTRAL_SYNTH_MODEL — для synth при primary=mistral (по умолчанию mistral-large-latest)

  Per-role модели OpenRouter (используются когда AI_DEBATE_PRIMARY=openrouter
  или mixed где роль попадает на openrouter). Все опциональны, дефолты подобраны
  под лучшие свободные :free модели на OpenRouter:
    OPENROUTER_BULL_MODEL      — дефолт openai/gpt-oss-120b:free
                                  (был nemotron-3-super-120b-a12b:free, но он
                                  reasoning-модель и сливал chain-of-thought
                                  в content + ответы на английском вместо
                                  русского — гонял на нём 5 раундов smoke-теста)
    OPENROUTER_BEAR_MODEL      — дефолт minimax/minimax-m2.5:free
    OPENROUTER_VERIFIER_MODEL  — дефолт openai/gpt-oss-120b:free
    OPENROUTER_SYNTH_MODEL     — дефолт inclusionai/ring-2.6-1t:free (1T params, agentic)

Fallback цепь: Cerebras → Groq → Mistral → OpenRouter → Together → Gemini
Цепочка кэйсов автоматическая при rate limits (429/402).
"""

import logging
import os
import re
import time
import asyncio
import aiohttp

from config import MAX_TOKENS_PER_AGENT, AGENT_TEMPERATURE

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=180)

# ── Ключи ──────────────────────────────────────────────────────────────────────
MISTRAL_API_KEY    = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_API_KEY_2  = os.getenv("MISTRAL_API_KEY_2", "")   # резервный Mistral
MISTRAL_MODEL      = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
MISTRAL_URL        = "https://api.mistral.ai/v1/chat/completions"

# OpenRouter — динамическое сканирование env. Поддерживаем неограниченное
# число ключей: OPENROUTER_API_KEY (главный) + OPENROUTER_API_KEY_2,
# OPENROUTER_API_KEY_3, ... (без верхнего лимита). Раньше был хардкод 4 слота;
# юзеру нужен жирный fallback (10-12+ ключей) — поэтому собираем динамически.
OPENROUTER_API_KEY   = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_KEY_2 = os.getenv("OPENROUTER_API_KEY_2", "")
OPENROUTER_API_KEY_3 = os.getenv("OPENROUTER_API_KEY_3", "")  # резервный OpenRouter #3
OPENROUTER_API_KEY_4 = os.getenv("OPENROUTER_API_KEY_4", "")  # резервный OpenRouter #4
OPENROUTER_URL       = "https://openrouter.ai/api/v1/chat/completions"

# Битые ключи помечаем сюда (по имени) при 401/«User not found» — чтобы
# на следующем вызове не тратить время и сразу прыгнуть на следующий слот.
# Сбрасывается с рестартом процесса (Railway деплой = новый сброс).
_OR_BROKEN_KEYS: set = set()


def _collect_openrouter_keys() -> list:
    """Собрать все OPENROUTER_API_KEY[_N] из env в порядке приоритета.

    Возвращает [(slot_name, key_value), ...] где slot_name — человекочитаемое
    имя слота для логов («OpenRouter», «OpenRouter#5» и т.п.). Skip'аем
    битые ключи (помеченные ранее в _OR_BROKEN_KEYS).
    """
    import re
    keys = []
    # Основной слот без суффикса
    main = os.getenv("OPENROUTER_API_KEY", "").strip()
    if main and "OpenRouter" not in _OR_BROKEN_KEYS:
        keys.append(("OpenRouter", main))
    # Сканируем все OPENROUTER_API_KEY_<N> где N — целое число.
    # Сортируем по N для предсказуемого порядка.
    numbered: list = []
    for env_name, env_val in os.environ.items():
        m = re.fullmatch(r"OPENROUTER_API_KEY_(\d+)", env_name)
        if m and env_val.strip():
            numbered.append((int(m.group(1)), env_val.strip()))
    for n, val in sorted(numbered):
        slot = f"OpenRouter#{n}"
        if slot not in _OR_BROKEN_KEYS:
            keys.append((slot, val))
    return keys

TOGETHER_API_KEY   = os.getenv("TOGETHER_API_KEY", "")
TOGETHER_API_KEY_2 = os.getenv("TOGETHER_API_KEY_2", "")  # резервный Together
TOGETHER_URL       = "https://api.together.xyz/v1/chat/completions"

GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")
GROQ_API_KEY_2     = os.getenv("GROQ_API_KEY_2", "")   # второй аккаунт
GROQ_API_KEY_3     = os.getenv("GROQ_API_KEY_3", "")   # третий аккаунт
GROQ_URL           = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL         = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"

# Базовая дефолтная модель — используется когда per-role override не задан.
# Подобрано из реальных свободных моделей OpenRouter (см. PR где это вводили).
# NOTE: дефолтную базовую модель сменили с nemotron-3-super-120b на gpt-oss-120b.
# Nemotron Super — reasoning-модель и в Round 1 Bull-агента сливала свою
# внутреннюю цепочку рассуждений прямо в контент ("We need to produce Bull
# Researcher output..." вместо нормального ответа), плюс часто отвечала на
# английском игнорируя русскоязычный системный промпт. gpt-oss-120b на тех же
# промптах в 5 проверках выдавал стабильный структурированный русский без
# leak'а reasoning_tokens.
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openai/gpt-oss-120b:free"
).strip() or "openai/gpt-oss-120b:free"

# Per-role override. Юзер может выставить разные модели на каждую роль:
#   OPENROUTER_BULL_MODEL, OPENROUTER_BEAR_MODEL,
#   OPENROUTER_VERIFIER_MODEL, OPENROUTER_SYNTH_MODEL.
# Если переменная не задана — fallback на OPENROUTER_MODEL (для bull/bear/verifier)
# или на OPENROUTER_MODEL (для synth тоже). Раньше synth был просто == bull/bear.
OPENROUTER_BULL_MODEL     = os.getenv("OPENROUTER_BULL_MODEL", "").strip()     or OPENROUTER_MODEL
OPENROUTER_BEAR_MODEL     = os.getenv("OPENROUTER_BEAR_MODEL", "").strip()     or "minimax/minimax-m2.5:free"
OPENROUTER_VERIFIER_MODEL = os.getenv("OPENROUTER_VERIFIER_MODEL", "").strip() or "openai/gpt-oss-120b:free"
OPENROUTER_SYNTH_MODEL    = os.getenv("OPENROUTER_SYNTH_MODEL", "").strip()    or "inclusionai/ring-2.6-1t:free"

TOGETHER_MODEL = os.getenv(
    "TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"
).strip() or "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip() or "gemini-1.5-flash"

# ── Cerebras (бесплатно!) ───────────────────────────────────────────────────────
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL   = os.getenv("CEREBRAS_MODEL", "llama-3.1-70b").strip() or "llama-3.1-70b"
CEREBRAS_URL     = "https://api.cerebras.ai/v1/chat/completions"

# ── Трекинг моделей для честного лейбла в отчёте ─────────────────────────────
MODELS_USED: dict = {}  # {"bull": "Mistral Small", "synth": "Mistral Large", ...}

# ── Трекинг использования (токены / вызовы по провайдеру) ───────────────────
# {provider_name: {"calls": int, "prompt_tokens": int, "completion_tokens": int,
#                  "total_tokens": int, "by_model": {model: same dict}}}
USAGE_STATS: dict = {}


def _track_usage(provider: str, model: str, usage: dict | None):
    """Запись usage data из API response. Безопасна к None/пустой usage."""
    if not provider:
        return
    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    tt = int((usage or {}).get("total_tokens") or (pt + ct))
    
    p = USAGE_STATS.setdefault(provider, {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "by_model": {},
    })
    p["calls"] += 1
    p["prompt_tokens"] += pt
    p["completion_tokens"] += ct
    p["total_tokens"] += tt
    
    if model:
        m = p["by_model"].setdefault(model, {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        })
        m["calls"] += 1
        m["prompt_tokens"] += pt
        m["completion_tokens"] += ct
        m["total_tokens"] += tt


def get_usage_stats() -> dict:
    """Snapshot текущей статистики usage."""
    return {
        provider: {
            "calls": data["calls"],
            "prompt_tokens": data["prompt_tokens"],
            "completion_tokens": data["completion_tokens"],
            "total_tokens": data["total_tokens"],
            "by_model": dict(data.get("by_model", {})),
        }
        for provider, data in USAGE_STATS.items()
    }


def reset_usage_stats() -> None:
    USAGE_STATS.clear()

def _track_model(agent_key: str, provider: str, model: str):
    labels = {
        "llama-3.1-70b":                                       "Cerebras/Llama 3.1 70B 🚀",
        "mistral-small-latest":                                 "Mistral Small",
        "mistral-large-latest":                                 "Mistral Large",
        "llama-3.3-70b-versatile":                              "Groq/Llama 3.3 70B",
        "meta-llama/llama-3.3-70b-instruct:free":               "OpenRouter/Llama 3.3 70B",
        "google/gemma-3-27b-it:free":                           "OpenRouter/Gemma 3 27B",
        "google/gemma-4-31b-it:free":                           "OpenRouter/Gemma 4 31B",
        "openai/gpt-oss-20b:free":                              "OpenRouter/gpt-oss 20B",
        # Free-tier подборка для дебатов (PR /openrouter-free-models):
        "nvidia/nemotron-3-super-120b-a12b:free":               "OpenRouter/Nemotron 3 Super 120B",
        "minimax/minimax-m2.5:free":                            "OpenRouter/MiniMax M2.5",
        "openai/gpt-oss-120b:free":                             "OpenRouter/gpt-oss 120B",
        "inclusionai/ring-2.6-1t:free":                         "OpenRouter/Ring 2.6 1T 🧠",
        "qwen/qwen3-next-80b-a3b-instruct:free":                "OpenRouter/Qwen3 Next 80B",
    }
    label = labels.get(model, f"{provider}/{model}")
    MODELS_USED[agent_key] = label
    logger.info(f"[{agent_key}] использует: {label}")


def _debate_primary_env() -> str:
    return os.getenv("AI_DEBATE_PRIMARY", "mistral").strip().lower() or "mistral"


def _can_use_primary(name: str) -> bool:
    if name == "cerebras":
        return bool(CEREBRAS_API_KEY)
    if name == "mistral":
        return bool(MISTRAL_API_KEY or MISTRAL_API_KEY_2)
    if name == "groq":
        return bool(GROQ_API_KEY or GROQ_API_KEY_2 or GROQ_API_KEY_3)
    if name == "openrouter":
        return bool(_collect_openrouter_keys())
    if name == "together":
        return bool(TOGETHER_API_KEY or TOGETHER_API_KEY_2)
    if name == "gemini":
        return bool(GEMINI_API_KEY)
    return False


def _resolve_agent_models() -> dict:
    """Кто первым обрабатывает дебаты (остальное — fallback в _call_best_available)."""
    want = _debate_primary_env()
    if want not in ("cerebras", "mistral", "groq", "openrouter", "together", "gemini", "mixed"):
        logger.warning("AI_DEBATE_PRIMARY=%s неизвестен — использую cerebras/mistral", want)
        want = "cerebras" if _can_use_primary("cerebras") else "mistral"
    if want != "mixed" and not _can_use_primary(want):
        logger.warning(
            "AI_DEBATE_PRIMARY=%s недоступен (нет ключа) — откат на cerebras/mistral/groq",
            want,
        )
        if _can_use_primary("cerebras"):
            want = "cerebras"
        elif _can_use_primary("mistral"):
            want = "mistral"
        elif _can_use_primary("groq"):
            want = "groq"
        elif _can_use_primary("openrouter"):
            want = "openrouter"
        elif _can_use_primary("together"):
            want = "together"
        elif _can_use_primary("gemini"):
            want = "gemini"
        else:
            want = next(
                (n for n in ("cerebras", "groq", "openrouter", "together", "gemini", "mistral")
                 if _can_use_primary(n)),
                "mistral",
            )

    mm = os.getenv("MISTRAL_MODEL", MISTRAL_MODEL).strip() or MISTRAL_MODEL
    syn_m = os.getenv("MISTRAL_SYNTH_MODEL", "mistral-large-latest").strip() or "mistral-large-latest"

    if want == "cerebras":
        m = {"bull": {"provider": "cerebras", "model": CEREBRAS_MODEL},
             "verifier": {"provider": "cerebras", "model": CEREBRAS_MODEL},
             "bear": {"provider": "cerebras", "model": CEREBRAS_MODEL},
             "synth": {"provider": "cerebras", "model": CEREBRAS_MODEL}}
    elif want == "groq":
        m = {"bull": {"provider": "groq", "model": GROQ_MODEL},
             "verifier": {"provider": "groq", "model": GROQ_MODEL},
             "bear": {"provider": "groq", "model": GROQ_MODEL},
             "synth": {"provider": "groq", "model": GROQ_MODEL}}
    elif want == "openrouter":
        # Per-role: каждая роль идёт на свою модель (см. OPENROUTER_*_MODEL выше).
        m = {"bull":     {"provider": "openrouter", "model": OPENROUTER_BULL_MODEL},
             "bear":     {"provider": "openrouter", "model": OPENROUTER_BEAR_MODEL},
             "verifier": {"provider": "openrouter", "model": OPENROUTER_VERIFIER_MODEL},
             "synth":    {"provider": "openrouter", "model": OPENROUTER_SYNTH_MODEL}}
    elif want == "together":
        m = {"bull": {"provider": "together", "model": TOGETHER_MODEL},
             "verifier": {"provider": "together", "model": TOGETHER_MODEL},
             "bear": {"provider": "together", "model": TOGETHER_MODEL},
             "synth": {"provider": "together", "model": TOGETHER_MODEL}}
    elif want == "gemini":
        m = {"bull": {"provider": "gemini", "model": GEMINI_MODEL},
             "verifier": {"provider": "gemini", "model": GEMINI_MODEL},
             "bear": {"provider": "gemini", "model": GEMINI_MODEL},
             "synth": {"provider": "gemini", "model": GEMINI_MODEL}}
    elif want == "mixed":
        # Лучшие бесплатные модели для каждой роли:
        bull_p = "groq"       if _can_use_primary("groq")       else "cerebras" if _can_use_primary("cerebras") else "mistral"
        bear_p = "groq"       if _can_use_primary("groq")       else "cerebras" if _can_use_primary("cerebras") else "mistral"
        ver_p  = "cerebras"   if _can_use_primary("cerebras")   else "openrouter" if _can_use_primary("openrouter") else "groq"
        # Synth — самая важная роль, используем Gemini 2.5 Pro через OpenRouter!
        syn_p  = "openrouter" if _can_use_primary("openrouter") else "mistral" if _can_use_primary("mistral") else "groq"
    
        def _model_for(p, agent_key=None):
            if p == "cerebras":
                return CEREBRAS_MODEL
            if p == "groq":
                return GROQ_MODEL
            if p == "together":
                return TOGETHER_MODEL
            if p == "openrouter":
                # Per-role override: каждая роль на свою OpenRouter-модель.
                if agent_key == "bull":     return OPENROUTER_BULL_MODEL
                if agent_key == "bear":     return OPENROUTER_BEAR_MODEL
                if agent_key == "verifier": return OPENROUTER_VERIFIER_MODEL
                if agent_key == "synth":    return OPENROUTER_SYNTH_MODEL
                return OPENROUTER_MODEL
            if p == "mistral":
                return mm
            return GROQ_MODEL
    
        m = {
            "bull":     {"provider": bull_p, "model": _model_for(bull_p, "bull")},
            "bear":     {"provider": bear_p, "model": _model_for(bear_p, "bear")},
            "verifier": {"provider": ver_p,  "model": _model_for(ver_p, "verifier")},
            "synth":    {"provider": syn_p,  "model": _model_for(syn_p, "synth")},
        }
    else:
        m = {"bull": {"provider": "mistral", "model": mm},
             "verifier": {"provider": "mistral", "model": mm},
             "bear": {"provider": "mistral", "model": mm},
             "synth": {"provider": "mistral", "model": syn_m}}

    logger.info("Дебаты: первичный провайдер = %s (AI_DEBATE_PRIMARY)", want)
    return m


def get_models_summary() -> str:
    if not MODELS_USED:
        return "🐂 Bull | 🐻 Bear | 🔍 Verifier | ⚖️ Synth"
    bull     = MODELS_USED.get("bull", "?")
    bear     = MODELS_USED.get("bear", "?")
    verifier = MODELS_USED.get("verifier", "?")
    synth    = MODELS_USED.get("synth", "?")
    return (
        f"🐂 Bull = {bull} | "
        f"🐻 Bear = {bear} | "
        f"🔍 Verifier = {verifier} | "
        f"⚖️ Synth = {synth}"
    )


# ── Модели по агентам (первый ход дебатов) ────────────────────────────────────
AGENT_MODELS = _resolve_agent_models()

_AGENT_MAX_TOKENS = {
    "bull":     2500,   # увеличено — 4 возможности Russia Edge не обрезаются
    "bear":     2500,   # увеличено — 4 риска Russia Edge не обрезаются
    "verifier": 1000,
    "synth":    3500,   # Synth needs room for verdict + trading plan + trigger + reminders
}

# ── Hallucination tracking per agent ─────────────────────────────────────────
# Tracks: agent → total_args, hallucinations, per-model stats
import threading as _threading

_hallucination_lock = _threading.Lock()
_hallucination_stats = {
    "bull":     {"total": 0, "hall": 0, "by_model": {}},
    "bear":     {"total": 0, "hall": 0, "by_model": {}},
    "verifier": {"total": 0, "hall": 0, "by_model": {}},
    "synth":    {"total": 0, "hall": 0, "by_model": {}},
}


def track_hallucinations(agent_key: str, total_args: int, hallucination_count: int, model_name: str = ""):
    """Call this after each agent response to track hallucination rate."""
    with _hallucination_lock:
        if agent_key not in _hallucination_stats:
            return
        s = _hallucination_stats[agent_key]
        s["total"] += total_args
        s["hall"] += hallucination_count
        if model_name:
            if model_name not in s["by_model"]:
                s["by_model"][model_name] = {"total": 0, "hall": 0}
            s["by_model"][model_name]["total"] += total_args
            s["by_model"][model_name]["hall"] += hallucination_count


def get_hallucination_report() -> dict:
    """Returns hallucination stats for all agents. Call after debate."""
    with _hallucination_lock:
        report = {}
        for agent, s in _hallucination_stats.items():
            rate = (s["hall"] / s["total"] * 100) if s["total"] > 0 else 0.0
            by_m = {}
            for m, ms in s["by_model"].items():
                m_rate = (ms["hall"] / ms["total"] * 100) if ms["total"] > 0 else 0.0
                by_m[m] = {"rate": round(m_rate, 1), "total_args": ms["total"], "hallucinations": ms["hall"]}
            report[agent] = {
                "total_args": s["total"],
                "hallucinations": s["hall"],
                "rate_pct": round(rate, 1),
                "by_model": by_m,
            }
        return report


def log_hallucination_stats():
    """Logs current hallucination stats to logger. Call periodically."""
    report = get_hallucination_report()
    for agent, data in report.items():
        rate = data["rate_pct"]
        emoji = "🟢" if rate < 10 else "🟡" if rate < 25 else "🔴"
        logger.info(f"[HALLUCINATION] {agent}: {data['hallucinations']}/{data['total_args']} ({rate:.1f}%) {emoji}")
        for m, md in data["by_model"].items():
            logger.info(f"  → {m}: {md['hallucinations']}/{md['total_args']} ({md['rate']:.1f}%)")


# ── Базовый вызов ──────────────────────────────────────────────────────────────

async def _call_openai_style(
    url: str, api_key: str, model: str,
    prompt: str, system: str, temperature: float, name: str,
    extra_headers: dict = None, agent_key: str = None
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    max_tok = _AGENT_MAX_TOKENS.get(agent_key, MAX_TOKENS_PER_AGENT)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": min(temperature, 1.0),
        "max_tokens": max_tok,
    }

    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"{name} HTTP {resp.status}: {err[:300]}")
            data = await resp.json()
            try:
                _track_usage(name, model, data.get("usage"))
            except Exception:
                pass
            # Защитный разбор: MiniMax/некоторые OR-модели могут вернуть
            # 200 OK с {"choices": []} или {"choices": [{"message": {"content": null}}]}.
            # Раньше это крашилось как 'NoneType' object has no attribute 'strip' и
            # сжигало ключ из очереди впустую. Теперь явный RuntimeError → fallback
            # на следующий ключ/провайдер.
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(
                    f"{name} no choices in response. Raw: {str(data)[:300]}"
                )
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if not content:
                raise RuntimeError(
                    f"{name} empty content. Raw: {str(data)[:300]}"
                )
            return content.strip()


# ── Провайдеры ────────────────────────────────────────────────────────────────

async def _call_cerebras(prompt: str, system: str, temperature: float,
                         model: str = None, agent_key: str = None) -> str:
    """Cerebras — бесплатная быстрая API."""
    if not CEREBRAS_API_KEY:
        raise ValueError("Нет CEREBRAS_API_KEY")
    
    m = model or CEREBRAS_MODEL
    try:
        result = await _call_openai_style(
            CEREBRAS_URL, CEREBRAS_API_KEY, m,
            prompt, system, temperature, "Cerebras",
            agent_key=agent_key
        )
        if agent_key:
            _track_model(agent_key, "Cerebras", m)
        logger.info(f"Cerebras ✅")
        return result
    except RuntimeError as e:
        logger.warning(f"Cerebras ❌: {e}")
        raise


async def _call_groq(prompt: str, system: str, temperature: float,
                     model: str = None, agent_key: str = None) -> str:
    """Groq#1 → Groq#2 при 429."""
    if not GROQ_API_KEY and not GROQ_API_KEY_2 and not GROQ_API_KEY_3:
        raise ValueError("Нет GROQ_API_KEY")

    m = model or GROQ_MODEL
    keys_to_try = []
    if GROQ_API_KEY:   keys_to_try.append(("Groq#1", GROQ_API_KEY))
    if GROQ_API_KEY_2: keys_to_try.append(("Groq#2", GROQ_API_KEY_2))
    if GROQ_API_KEY_3: keys_to_try.append(("Groq#3", GROQ_API_KEY_3))

    last_err = None
    for key_name, key in keys_to_try:
        try:
            result = await _call_openai_style(
                GROQ_URL, key, m, prompt, system, temperature, key_name,
                agent_key=agent_key
            )
            if agent_key:
                _track_model(agent_key, key_name, m)
            logger.info(f"Groq {key_name} ✅")
            return result
        except RuntimeError as e:
            err_s = str(e)
            if "429" in err_s:
                current_idx = keys_to_try.index((key_name, key))
                has_next = current_idx < len(keys_to_try) - 1
                if has_next:
                    # Есть следующий ключ — переключаемся СРАЗУ без ожидания
                    logger.warning(f"{key_name} лимит → сразу пробую следующий ключ...")
                    last_err = e
                    continue
                else:
                    # Последний ключ — ждём и повторяем
                    wait_m = re.search(r"try again in ([\d.]+)\s*s", err_s, re.I)
                    if wait_m:
                        sec = min(30.0, float(wait_m.group(1)) + 1.0)
                        logger.warning(
                            "%s последний ключ — жду %.1fs...",
                            key_name, sec,
                        )
                        await asyncio.sleep(sec)
                        try:
                            result = await _call_openai_style(
                                GROQ_URL, key, m, prompt, system, temperature,
                                key_name, agent_key=agent_key,
                            )
                            if agent_key:
                                _track_model(agent_key, key_name, m)
                            logger.info(f"Groq {key_name} ✅ (после паузы)")
                            return result
                        except RuntimeError as e2:
                            last_err = e2
                    logger.warning(f"{key_name} лимит исчерпан")
                    last_err = e
                    continue
            raise
    raise RuntimeError(f"Все Groq ключи исчерпаны. Последняя ошибка: {last_err}")


async def _call_mistral(prompt: str, system: str, temperature: float,
                        model: str = None, agent_key: str = None) -> str:
    """Mistral с автопереключением KEY_1 → KEY_2 при 429."""
    m = model or MISTRAL_MODEL
    keys_to_try = []
    if MISTRAL_API_KEY:   keys_to_try.append(("Mistral#1", MISTRAL_API_KEY))
    if MISTRAL_API_KEY_2: keys_to_try.append(("Mistral#2", MISTRAL_API_KEY_2))
    if not keys_to_try:
        raise ValueError("Нет MISTRAL_API_KEY")
    last_err = None
    for key_name, key in keys_to_try:
        try:
            result = await _call_openai_style(
                MISTRAL_URL, key, m,
                prompt, system, temperature, key_name,
                agent_key=agent_key
            )
            if agent_key:
                _track_model(agent_key, key_name, m)
            logger.info(f"{key_name} ✅")
            return result
        except RuntimeError as e:
            if "429" in str(e):
                logger.warning(f"{key_name} лимит — пробую Mistral#2...")
                last_err = e
                await asyncio.sleep(2.5)
                continue
            raise
    raise RuntimeError(f"Все Mistral ключи исчерпаны: {last_err}")


_OR_HEADERS = {
    "HTTP-Referer": "https://dialectic-edge.bot",
    "X-Title": "Dialectic Edge",
}


async def _call_openrouter_model(
    prompt: str,
    system: str,
    temperature: float,
    model: str,
    agent_key: str = None,
) -> str:
    """OpenRouter: автоматическая ротация по всем доступным ключам.

    Перебираем `_collect_openrouter_keys()` (динамический скан env). При
    429/402 (rate-limit / out-of-credits) — переход на следующий слот.
    При 401/«User not found» / «No auth credentials» — помечаем слот как
    битый в `_OR_BROKEN_KEYS` и больше его в этом процессе не дёргаем
    (быстрее на следующих вызовах).
    """
    keys_try = _collect_openrouter_keys()
    if not keys_try:
        raise ValueError("Нет OPENROUTER_API_KEY (ни один слот не заполнен)")
    last_err = None
    for key_name, key in keys_try:
        try:
            result = await _call_openai_style(
                OPENROUTER_URL, key, model,
                prompt, system, temperature, key_name,
                extra_headers=_OR_HEADERS,
                agent_key=agent_key,
            )
            if agent_key:
                _track_model(agent_key, key_name, model)
            return result
        except RuntimeError as e:
            err = str(e)
            # Лимит / out-of-credits — пробуем следующий ключ, не маркаем битым
            if "429" in err or "402" in err:
                logger.warning("%s лимит OpenRouter — следующий ключ...", key_name)
                last_err = e
                continue
            # Революция ключа / неверная авторизация — маркируем битым
            # навсегда (на эту сессию), чтобы не тратить RTT каждый запрос
            if "401" in err or "User not found" in err or "No auth" in err.lower():
                logger.warning(
                    "%s невалиден (%s) — помечаю битым для этой сессии",
                    key_name, err[:80]
                )
                _OR_BROKEN_KEYS.add(key_name)
                last_err = e
                continue
            raise
    raise RuntimeError(f"Все OpenRouter ключи исчерпаны: {last_err}")


async def _call_openrouter_llama(prompt: str, system: str, temperature: float,
                                  agent_key: str = None) -> str:
    return await _call_openrouter_model(
        prompt, system, temperature,
        "meta-llama/llama-3.3-70b-instruct:free",
        agent_key,
    )


async def _call_openrouter_gemma(prompt: str, system: str, temperature: float,
                                  agent_key: str = None) -> str:
    # Gemma 3 27b на OR с 11.05.2026 возвращает 404 "No endpoints found" —
    # перешли на Gemma 4 31b:free (живая, 262k контекст).
    return await _call_openrouter_model(
        prompt, system, temperature,
        "google/gemma-4-31b-it:free",
        agent_key,
    )


async def _call_openrouter_gpt_oss_20b(prompt: str, system: str, temperature: float,
                                        agent_key: str = None) -> str:
    # gpt-oss-20b — облегчённая версия 120B. Меньше рейт-лимитов на upstream,
    # тот же стиль ответа. Используем как ещё один free-вариант когда Llama
    # упёрлась в upstream rate-limit (Venice провайдер).
    return await _call_openrouter_model(
        prompt, system, temperature,
        "openai/gpt-oss-20b:free",
        agent_key,
    )


async def _call_openrouter_gemini(prompt: str, system: str, temperature: float,
                                  agent_key: str = None) -> str:
    """Gemini через OpenRouter — используем 2.5 Pro для Synth, 2.0 Flash для остальных."""
    model = "google/gemini-2.5-pro" if agent_key == "synth" else "google/gemini-2.0-flash-001"
    
    return await _call_openrouter_model(
        prompt, system, temperature,
        model,
        agent_key,
    )


async def _call_gemini(
    prompt: str, system: str, temperature: float, agent_key: str = None
) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("Нет GEMINI_API_KEY")
    m = GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
    max_tok = _AGENT_MAX_TOKENS.get(agent_key, MAX_TOKENS_PER_AGENT)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": min(temperature, 1.0),
            "maxOutputTokens": max_tok,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    params = {"key": GEMINI_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, params=params, json=body, timeout=TIMEOUT) as resp:
            raw = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Gemini HTTP {resp.status}: {raw[:400]}")
            data = await resp.json()
            cand = data.get("candidates") or []
            if not cand:
                raise RuntimeError(f"Gemini: нет candidates — {raw[:250]}")
            parts = cand[0].get("content", {}).get("parts") or []
            if not parts or not parts[0].get("text"):
                raise RuntimeError("Gemini: пустой текст")
            out = parts[0]["text"].strip()
            if agent_key:
                _track_model(agent_key, "Gemini", m)
            return out


async def _call_together(
    prompt: str,
    system: str,
    temperature: float,
    model: str = None,
    agent_key: str = None,
) -> str:
    """Together AI — KEY_1 → KEY_2 при 429."""
    m = model or TOGETHER_MODEL
    keys_to_try = []
    if TOGETHER_API_KEY:   keys_to_try.append(("Together#1", TOGETHER_API_KEY))
    if TOGETHER_API_KEY_2: keys_to_try.append(("Together#2", TOGETHER_API_KEY_2))
    if not keys_to_try:
        raise ValueError("Нет TOGETHER_API_KEY")

    last_err = None
    for key_name, key in keys_to_try:
        try:
            result = await _call_openai_style(
                TOGETHER_URL, key, m,
                prompt, system, temperature, key_name,
                agent_key=agent_key
            )
            if agent_key:
                _track_model(agent_key, key_name, m)
            logger.info(f"{key_name} ✅")
            return result
        except RuntimeError as e:
            if "429" in str(e):
                logger.warning(f"{key_name} лимит — пробую Together#2...")
                last_err = e
                continue
            raise
    raise RuntimeError(f"Все Together ключи исчерпаны: {last_err}")


# ── Throttle для Mistral ──────────────────────────────────────────────────────
_LAST_MISTRAL_CALL = 0.0
_mistral_lock = None

def _get_mistral_lock() -> asyncio.Lock:
    global _mistral_lock
    if _mistral_lock is None:
        _mistral_lock = asyncio.Lock()
    return _mistral_lock

async def _call_mistral_throttled(prompt: str, system: str, temperature: float,
                                   model: str = None, agent_key: str = None) -> str:
    global _LAST_MISTRAL_CALL
    lock = _get_mistral_lock()
    async with lock:
        now = time.time()
        wait = 3.0 - (now - _LAST_MISTRAL_CALL)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_MISTRAL_CALL = time.time()
    return await _call_mistral(prompt, system, temperature, model, agent_key=agent_key)


# ── Роутер ────────────────────────────────────────────────────────────────────

async def _call_for_agent(
    agent_key: str,
    prompt: str,
    system: str,
    temperature: float,
    *,
    skip_primary: bool = False,
) -> str:
    """Wrapper: записывает (provider, model, role, latency, ok) в ai_call_metrics.

    Делегирует на :func:`_call_for_agent_impl`. Любая ошибка/успех
    логируются как одна строка в SQLite (через ``core.ai_metrics``),
    провал записи метрик не должен влиять на debate-loop — поэтому
    обёрнуто в широкий ``except Exception: pass``.
    """
    config = AGENT_MODELS.get(agent_key)
    started = time.monotonic()
    primary_provider = config["provider"] if config else "fallback"
    primary_model = config["model"] if config else None
    try:
        result = await _call_for_agent_impl(
            agent_key, prompt, system, temperature, skip_primary=skip_primary
        )
    except Exception as exc:
        try:
            from core.ai_metrics import record_ai_call
            await record_ai_call(
                provider=primary_provider, model=primary_model, role=agent_key,
                latency_ms=int((time.monotonic() - started) * 1000), ok=False,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        except Exception:
            pass
        raise
    try:
        from core.ai_metrics import record_ai_call
        await record_ai_call(
            provider=primary_provider, model=primary_model, role=agent_key,
            latency_ms=int((time.monotonic() - started) * 1000), ok=True,
        )
    except Exception:
        pass
    return result


async def _call_for_agent_impl(
    agent_key: str,
    prompt: str,
    system: str,
    temperature: float,
    *,
    skip_primary: bool = False,
) -> str:
    """Получить ответ от агента (bull/bear/verifier/synth).

    skip_primary=True пропускает первичную модель из AGENT_MODELS и
    сразу идёт в _call_best_available. Полезно при retry после CoT-leak'а:
    первичная reasoning-модель (Nemotron/gpt-oss/MiniMax) вернула свои
    «мысли» вместо ответа → нужно взять другую модель без повтора той же.
    При skip_primary=True также скипаем openrouter в fallback'е (большинство
    leak'ов приходит от OR reasoning-моделей).
    """
    config = AGENT_MODELS.get(agent_key)

    if skip_primary:
        # Раньше тут было skip_providers={"openrouter"} — то есть при leak'е
        # реасонера на OR (Nemotron 120B / gpt-oss / MiniMax) мы СРАЗУ
        # уходили на Cerebras. Это слишком жёстко: на OR есть и не-leaking
        # модели (Llama 3.3 70B, Gemma 4 31B), у юзера 12 OR-ключей,
        # ёмкость огромная. Сейчас пробуем сначала non-reasoning OR-модели
        # (Llama → Gemma 4 → Gemini), и только если ВСЕ они упали или
        # тоже leakнули — идём в Cerebras/Groq/Mistral.
        try:
            return await _call_openrouter_llama(
                prompt, system, temperature, agent_key=agent_key
            )
        except Exception as e:
            logger.warning("[%s] retry OR/Llama ❌ %s", agent_key, str(e)[:120])
        try:
            return await _call_openrouter_gemma(
                prompt, system, temperature, agent_key=agent_key
            )
        except Exception as e:
            logger.warning("[%s] retry OR/Gemma 4 ❌ %s", agent_key, str(e)[:120])
        try:
            return await _call_openrouter_gemini(
                prompt, system, temperature, agent_key=agent_key
            )
        except Exception as e:
            logger.warning("[%s] retry OR/Gemini ❌ %s", agent_key, str(e)[:120])
        # Все OR-альтернативы упали — идём на не-OR провайдеры.
        return await _call_best_available(
            prompt, system, temperature, agent_key,
            skip_providers=frozenset({"openrouter"}),
        )

    if config:
        provider = config["provider"]
        model    = config["model"]
        try:
            if provider == "cerebras":
                result = await _call_cerebras(prompt, system, temperature, model, agent_key=agent_key)
            elif provider == "mistral":
                result = await _call_mistral_throttled(prompt, system, temperature, model, agent_key=agent_key)
            elif provider == "groq":
                result = await _call_groq(prompt, system, temperature, model, agent_key=agent_key)
            elif provider == "openrouter":
                # Если модель Gemini — используем специализированную функцию
                if model and "gemini" in model.lower():
                    result = await _call_openrouter_gemini(
                        prompt, system, temperature, agent_key=agent_key
                    )
                else:
                    result = await _call_openrouter_model(
                        prompt, system, temperature, model, agent_key=agent_key
                    )
            elif provider == "together":
                result = await _call_together(
                    prompt, system, temperature, model, agent_key=agent_key
                )
            elif provider == "gemini":
                result = await _call_gemini(prompt, system, temperature, agent_key=agent_key)
            else:
                raise ValueError(f"Неизвестный провайдер: {provider}")
            logger.info(f"[{agent_key}] → {provider}/{model} ✅")
            return result
        except Exception as e:
            logger.warning(f"[{agent_key}] → {provider}/{model} ❌ {e}")

        # Synth: только для Mistral Large → пробуем Small
        if (
            agent_key == "synth"
            and provider == "mistral"
            and model
            and "large" in model.lower()
        ):
            try:
                result = await _call_mistral_throttled(
                    prompt, system, temperature, "mistral-small-latest", agent_key=agent_key
                )
                logger.info(f"[{agent_key}] fallback → mistral-small ✅")
                return result
            except Exception as e2:
                logger.warning(f"[{agent_key}] synth mistral-small ❌ {e2}")

    # Если primary был OpenRouter — НЕ блокируем весь провайдер: у юзера
    # 12 ключей и есть альтернативные free-модели (Llama 3.3, Gemma, Gemini
    # через OR). Сбой обычно модельный (rate-limit на конкретной модели),
    # а не «весь OR лёг» — поэтому даём другим OR-моделям шанс.
    if config and config.get("provider") == "openrouter":
        skip_p = frozenset()
    else:
        skip_p = frozenset({config["provider"]} if config else [])
    return await _call_best_available(
        prompt, system, temperature, agent_key,
        skip_providers=skip_p,
    )


async def _call_best_available(
    prompt: str,
    system: str,
    temperature: float,
    agent_name: str = "general",
    *,
    skip_providers: frozenset | None = None,
) -> str:
    """
    Цепочка fallback: OpenRouter (12 ключей!) → Cerebras → Groq → Mistral → Together → Gemini

    Порядок раньше был Cerebras → Groq → ... → OpenRouter, но у юзера 12 OR-ключей
    (= наибольшая ёмкость по запросам), а Cerebras всего один и часто упирается
    в дневной лимит свободного тира. Поэтому OR в fallback'е сейчас первый.

    skip_providers — не вызывать тот же API повторно (primary уже отработал или упал).
    """
    skip = set(skip_providers or [])

    providers = []
    # OpenRouter — приоритет №1: 12 ключей × несколько free-моделей даёт самую
    # большую ёмкость. Llama 3.3 70B как самый стабильный из free-моделей.
    if "openrouter" not in skip and _collect_openrouter_keys():
        # Free-tier OR-модели рейт-лимитятся UPSTREAM (Venice/Llama, например).
        # Когда Llama 3.3 в апстриме упирается в лимит — 12 наших ключей это
        # не починят (это global cap у провайдера, не per-key). Поэтому даём
        # сразу несколько разных upstream'ов: Llama (Venice) → Gemma 4
        # (Google) → gpt-oss 20b (OpenAI-самобэкэнд) → Gemini-paid. При лимите
        # на одной модели соседняя обычно ещё жива.
        providers.append(("OpenRouter/Llama",
            lambda p, s, t: _call_openrouter_llama(p, s, t, agent_key=agent_name)))
        providers.append(("OpenRouter/Gemma",
            lambda p, s, t: _call_openrouter_gemma(p, s, t, agent_key=agent_name)))
        providers.append(("OpenRouter/gpt-oss-20b",
            lambda p, s, t: _call_openrouter_gpt_oss_20b(p, s, t, agent_key=agent_name)))
        # Gemini через OpenRouter — платная (но дешёвая) и почти никогда не
        # упирается в лимит. Идёт ПОСЛЕ free-моделей, чтобы не жечь кредиты
        # без нужды.
        providers.append(("OpenRouter/Gemini",
            lambda p, s, t: _call_openrouter_gemini(p, s, t, agent_key=agent_name)))

    if "cerebras" not in skip and CEREBRAS_API_KEY:
        providers.append(("Cerebras/Llama 3.3 70B",
            lambda p, s, t: _call_cerebras(p, s, t, agent_key=agent_name)))

    if "groq" not in skip and (GROQ_API_KEY or GROQ_API_KEY_2 or GROQ_API_KEY_3):
        providers.append(("Groq/Llama",
            lambda p, s, t: _call_groq(p, s, t, agent_key=agent_name)))

    if "mistral" not in skip and (MISTRAL_API_KEY or MISTRAL_API_KEY_2):
        providers.append(("Mistral Small",
            lambda p, s, t: _call_mistral_throttled(p, s, t, agent_key=agent_name)))

    if "together" not in skip and (TOGETHER_API_KEY or TOGETHER_API_KEY_2):
        providers.append(("Together/Llama",
            lambda p, s, t: _call_together(p, s, t, agent_key=agent_name)))

    if "gemini" not in skip and GEMINI_API_KEY:
        providers.append(("Gemini",
            lambda p, s, t: _call_gemini(p, s, t, agent_key=agent_name)))

    if not providers:
        raise ValueError("Нет API ключей! Добавь CEREBRAS_API_KEY, GROQ_API_KEY и/или MISTRAL_API_KEY")

    last_error = None
    for name, caller in providers:
        try:
            result = await caller(prompt, system, temperature)
            logger.info(f"[{agent_name}] fallback → {name} ✅")
            return result
        except Exception as e:
            logger.warning(f"[{agent_name}] fallback → {name} ❌ {e}")
            last_error = e

    raise RuntimeError(f"Все провайдеры недоступны. Последняя ошибка: {last_error}")


# ── Публичный класс ───────────────────────────────────────────────────────────

class AgentProvider:

    async def bull(self, prompt: str, system: str = "", temperature: float = None,
                   skip_primary: bool = False) -> str:
        t = temperature or AGENT_TEMPERATURE
        return await _call_for_agent("bull", prompt, system, t, skip_primary=skip_primary)

    async def bear(self, prompt: str, system: str = "", temperature: float = None,
                   skip_primary: bool = False) -> str:
        t = (temperature or AGENT_TEMPERATURE) * 0.4
        return await _call_for_agent("bear", prompt, system, t, skip_primary=skip_primary)

    async def verifier(self, prompt: str, system: str = "", temperature: float = None,
                       skip_primary: bool = False) -> str:
        t = 0.1
        return await _call_for_agent("verifier", prompt, system, t, skip_primary=skip_primary)

    async def synth(self, prompt: str, system: str = "", temperature: float = None,
                    skip_primary: bool = False) -> str:
        t = (temperature or AGENT_TEMPERATURE) * 0.6
        return await _call_for_agent("synth", prompt, system, t, skip_primary=skip_primary)

    async def complete(self, prompt: str, system: str = "", temperature: float = None) -> str:
        t = temperature or AGENT_TEMPERATURE
        return await _call_best_available(prompt, system, t, "general", skip_providers=frozenset())


ai = AgentProvider()
