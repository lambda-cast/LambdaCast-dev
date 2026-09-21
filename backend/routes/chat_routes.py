"""
routes/chat_routes.py
======================
Chatbot integration with role-based context scoping.

Design
------
- Admins see: fleet-wide stats, all installations, all users
- Users see: only their own installations, their personal stats
- Uses local Ollama LLM for privacy (or can integrate OpenAI)
- Streaming responses for real-time feedback

Blueprint prefix: /api/chat

Examples
--------
POST /api/chat
  {
    "message": "What's the total capacity?",
    "stream": false
  }
  
Response (admin):
  {
    "role": "assistant",
    "content": "The fleet has 12.5 kWp capacity across 3 installations..."
  }

Response (user):
  {
    "role": "assistant", 
    "content": "Your Sfax installation has 1.0 kWp capacity..."
  }
"""

import logging
import json as _json
from flask import Blueprint, request, jsonify, g, stream_with_context, Response
import requests

from auth import require_auth
from platform_db import PlatformDatabase

chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")

# Configuration
OLLAMA_BASE_URL = "http://localhost:11434"  # Default Ollama endpoint
OLLAMA_MODEL = "llama2"  # Change to "mistral", "neural-chat", etc.
SYSTEM_PROMPT = """You are LambdaCast, a helpful solar energy assistant. 
You provide insights about solar installations, forecasts, and energy production.
Be concise, friendly, and accurate.
Always provide numbers in meaningful units (kW, kWh, %).
"""

# ---------------------------------------------------------------------------
# Context builders (role-aware)
# ---------------------------------------------------------------------------

def _build_admin_context(db: PlatformDatabase) -> str:
    """Build context for admin (fleet-wide view)."""
    stats = db.get_solar_statistics()
    users = db.list_users()
    
    context = f"""Fleet Statistics (Admin View):
- Total installations: {stats.get('total_installations', 0)}
- Total capacity: {stats.get('total_capacity_kwp', 0):.1f} kWp
- Active installations: {stats.get('active_count', 0)}
- Maintenance: {stats.get('maintenance_count', 0)}
- Total active users: {stats.get('total_active_users', 0)}
- Estimated today production: {stats.get('estimated_today_kwh', 0):.1f} kWh

You have access to all installations, users, and data across the platform.
Help answer questions about:
- Fleet capacity and performance
- Installation details and status
- User management
- Energy production forecasts
- System health and maintenance"""
    
    return context


def _build_user_context(user: dict, db: PlatformDatabase) -> str:
    """Build context for regular user (own installations only)."""
    installations = db.list_installations_for_user(user["id"])
    
    total_capacity = sum(i.get("installed_capacity_kwp") or 0 for i in installations)
    total_count = len(installations)
    
    inst_list = "\n".join([
        f"  - {i['name']} ({i.get('installed_capacity_kwp', 0):.2f} kWp, {i.get('status', 'active')})"
        for i in installations
    ])
    
    context = f"""Your Personal Solar Data (User View):
- Total installations: {total_count}
- Total capacity: {total_capacity:.1f} kWp
- Installations:
{inst_list}

You can answer questions about:
- Your installations' production and performance
- Energy forecasts
- System status
- Personal production goals

Note: You only see your own data. Contact an admin for fleet-wide information."""
    
    return context


# ---------------------------------------------------------------------------
# Message builders for LLM
# ---------------------------------------------------------------------------

def _build_chat_payload(user_message: str, context: str) -> list:
    """Build the messages array for the LLM."""
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + "\n\n" + context,
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------

def _call_ollama(messages: list, stream: bool = False) -> dict | str:
    """
    Call Ollama API for chat completion.
    
    Returns:
      - If stream=False: {"role": "assistant", "content": "..."}
      - If stream=True: generator yielding {"delta": {"content": "..."}}
    """
    try:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": stream,
            "temperature": 0.7,
        }
        
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=30,
            stream=stream,
        )
        response.raise_for_status()
        
        if not stream:
            data = response.json()
            return {
                "role": data.get("message", {}).get("role", "assistant"),
                "content": data.get("message", {}).get("content", ""),
            }
        
        # Streaming mode
        def event_stream():
            for line in response.iter_lines():
                if line:
                    chunk = _json.loads(line)
                    if chunk.get("message", {}).get("content"):
                        yield _json.dumps({
                            "delta": {
                                "content": chunk["message"]["content"]
                            }
                        }) + "\n"
        
        return event_stream()
        
    except requests.exceptions.ConnectionError:
        logging.error(f"Ollama connection failed at {OLLAMA_BASE_URL}")
        return {
            "role": "assistant",
            "content": "⚠️ Chat service unavailable. Is Ollama running? Try: docker-compose exec ollama ollama serve",
        }
    except requests.exceptions.Timeout:
        return {
            "role": "assistant",
            "content": "⏱️ Ollama response timeout. The model might be processing a long response.",
        }
    except Exception as e:
        logging.exception("Ollama API error")
        return {
            "role": "assistant",
            "content": f"❌ Chat error: {str(e)}",
        }


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@chat_bp.route("", methods=["POST"])
@require_auth
def post_chat():
    """
    POST /api/chat
    
    Body:
      {
        "message": "What's my production today?",
        "stream": false
      }
    
    Returns:
      - Non-streaming: {"role": "assistant", "content": "..."}
      - Streaming: Server-Sent Events (text/event-stream)
    """
    body = request.get_json(silent=True) or {}
    user_message = body.get("message", "").strip()
    stream = body.get("stream", False)
    
    if not user_message:
        return jsonify({"error": "message is required"}), 400
    
    if len(user_message) > 500:
        return jsonify({"error": "message too long (max 500 chars)"}), 400
    
    try:
        # Build role-aware context
        db = PlatformDatabase()
        user = g.current_user
        
        if user["role"] == "ADMIN":
            context = _build_admin_context(db)
        else:
            context = _build_user_context(user, db)
        
        # Build messages for LLM
        messages = _build_chat_payload(user_message, context)
        
        # Call LLM
        result = _call_ollama(messages, stream=stream)
        
        # Log conversation
        logging.info(
            f"Chat: user {user['username']} (role={user['role']}) | "
            f"msg={user_message[:50]}... | stream={stream}"
        )
        
        # Return result
        if stream:
            return Response(
                stream_with_context(result),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache"}
            )
        else:
            return jsonify(result)
    
    except Exception as e:
        logging.exception("Chat endpoint error")
        return jsonify({
            "error": "An unexpected error occurred",
            "details": str(e),
        }), 500


@chat_bp.route("/health", methods=["GET"])
@require_auth
def chat_health():
    """
    GET /api/chat/health
    Check if Ollama is running and which model is loaded.
    """
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        response.raise_for_status()
        data = response.json()
        
        models = [m.get("name", "unknown") for m in data.get("models", [])]
        model_loaded = OLLAMA_MODEL in models
        
        return jsonify({
            "status": "ok",
            "ollama_url": OLLAMA_BASE_URL,
            "available_models": models,
            "chat_model": OLLAMA_MODEL,
            "model_loaded": model_loaded,
        })
    
    except requests.exceptions.ConnectionError:
        return jsonify({
            "status": "unavailable",
            "error": f"Cannot connect to Ollama at {OLLAMA_BASE_URL}",
            "tips": [
                "Start Ollama: docker-compose up -d ollama",
                "Pull model: docker-compose exec ollama ollama pull " + OLLAMA_MODEL,
            ]
        }), 503
    
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
        }), 500


@chat_bp.route("/models", methods=["GET"])
@require_auth
def list_chat_models():
    """
    GET /api/chat/models
    List available Ollama models.
    Can change OLLAMA_MODEL in routes/chat_routes.py to switch models.
    """
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        response.raise_for_status()
        data = response.json()
        
        models = []
        for m in data.get("models", []):
            models.append({
                "name": m.get("name"),
                "modified_at": m.get("modified_at"),
                "size_gb": round(m.get("size", 0) / (1024**3), 1),
            })
        
        return jsonify({
            "status": "ok",
            "available_models": models,
            "current_chat_model": OLLAMA_MODEL,
        })
    
    except requests.exceptions.ConnectionError:
        return jsonify({
            "status": "unavailable",
            "error": "Ollama not running",
        }), 503
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
        }), 500
