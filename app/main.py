from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import os, time, json, base64, httpx

from email.mime.text import MIMEText
from pathlib import Path
from openai import OpenAI

# -------------------------------------------------------
# Load ENV
# -------------------------------------------------------
load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth/gmail/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
GMAIL_TEST_RECIPIENT = os.getenv("GMAIL_TEST_RECIPIENT")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# token file (single-user MVP)
TOKENS_FILE = Path("tokens.json")

def save_tokens(data: dict):
    expires_in = data.get("expires_in", 3600)
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expiry": time.time() + expires_in - 60,
    }
    TOKENS_FILE.write_text(json.dumps(tokens))

def load_tokens():
    if not TOKENS_FILE.exists():
        return None

    tokens = json.loads(TOKENS_FILE.read_text())
    if tokens["expiry"] > time.time():
        return tokens

    # refresh
    if not tokens.get("refresh_token"):
        return None

    refreshed = refresh_access_token(tokens["refresh_token"])
    return refreshed

def refresh_access_token(refresh_token: str):
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    resp = httpx.post("https://oauth2.googleapis.com/token", data=data)
    if resp.status_code != 200:
        return None

    token_data = resp.json()
    token_data["refresh_token"] = refresh_token
    save_tokens(token_data)
    return json.loads(TOKENS_FILE.read_text())

# -------------------------------------------------------
# FastAPI
# -------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------
# Health Route
# -------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

# -------------------------------------------------------
# Google OAuth Start
# -------------------------------------------------------
@app.get("/oauth/gmail/url")
def oauth_start():
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Google OAuth env vars missing")

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/gmail.send",
    }

    import urllib.parse
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return {"oauth_url": url}

# -------------------------------------------------------
# Google OAuth Callback
# -------------------------------------------------------
@app.get("/oauth/gmail/callback")
def oauth_callback(code: str):
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    resp = httpx.post("https://oauth2.googleapis.com/token", data=data)
    if resp.status_code != 200:
        raise HTTPException(400, resp.text)

    save_tokens(resp.json())

    return RedirectResponse(url=f"{FRONTEND_URL}/dashboard")

# -------------------------------------------------------
# Connection Status
# -------------------------------------------------------
@app.get("/connection-status")
def connection_status():
    tokens = load_tokens()
    if not tokens:
        return {"status": "not_connected"}
    return {"status": "connected"}

# -------------------------------------------------------
# Send Test Email
# -------------------------------------------------------
async def send_gmail_email(access_token: str, to: str, subject: str, body: str):
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"raw": raw}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers=headers,
            json=payload
        )

        if resp.status_code >= 400:
            raise HTTPException(500, resp.text)

class TestResult(BaseModel):
    detail: str

@app.post("/test-email", response_model=TestResult)
async def test_email():
    if not GMAIL_TEST_RECIPIENT:
        raise HTTPException(500, "Set GMAIL_TEST_RECIPIENT in .env")

    tokens = load_tokens()
    if not tokens:
        raise HTTPException(400, "Gmail not connected")

    await send_gmail_email(
        tokens["access_token"],
        GMAIL_TEST_RECIPIENT,
        "LiquidMail Test",
        "This is a test email from LiquidMail!"
    )

    return TestResult(detail="Email sent!")

# -------------------------------------------------------
# AI Reply
# -------------------------------------------------------
class ReplyRequest(BaseModel):
    sender_name: str | None = None
    email_text: str

@app.post("/generate-reply")
def generate_reply(req: ReplyRequest):
    prompt = f"""
    Write a natural, helpful reply in British English.

    Incoming email:
    {req.email_text}
    """

    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You are LiquidMail."},
            {"role": "user", "content": prompt},
        ],
    )

    return {"reply": completion.choices[0].message.content.strip()}
