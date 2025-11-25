from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
import os
import urllib.parse
import requests
import base64
from datetime import datetime, timedelta, timezone
from openai import OpenAI

# -------------------------------------------------------
# LOAD ENV
# -------------------------------------------------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI(title="LiquidMail API", version="2.0")

# -------------------------------------------------------
# CORS (Vercel + local dev)
# -------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------
# SCHEMAS
# -------------------------------------------------------
class GenerateReplyRequest(BaseModel):
    sender: str
    subject: str
    body: str

class SendEmailRequest(BaseModel):
    user_id: str
    to: str
    subject: str
    body: str

# -------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------

def save_tokens(user_id: str, tokens: dict):
    """Store Gmail tokens in Supabase"""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])

    supabase.table("gmail_tokens").insert({
        "user_email": user_id,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type", "Bearer"),
        "scope": tokens.get("scope"),
        "expires_at": expires_at.isoformat()
    }).execute()

def get_valid_access_token(user_email: str):
    """Retrieve latest valid Gmail token, or refresh it"""
    result = (
        supabase.table("gmail_tokens")
        .select("*")
        .eq("user_email", user_email)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not result.data:
        return None

    record = result.data[0]
    expires_at = datetime.fromisoformat(record["expires_at"])

    if expires_at > datetime.now(timezone.utc):   # still valid
        return record["access_token"]

    # refresh token
    res = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": record["refresh_token"],
            "grant_type": "refresh_token",
        }
    )
    new_tokens = res.json()

    if "access_token" not in new_tokens:
        return None

    save_tokens(user_email, new_tokens)
    return new_tokens["access_token"]

def send_gmail(user_email: str, to_addr: str, subject: str, body: str):
    """Send email using Gmail API"""
    access_token = get_valid_access_token(user_email)

    if not access_token:
        raise HTTPException(400, "No valid Gmail token found for this user")

    raw = f"To: {to_addr}\r\nSubject: {subject}\r\n\r\n{body}"
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()

    res = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        json={"raw": encoded},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    if res.status_code not in (200, 202):
        raise HTTPException(500, res.text)

    return res.json()

def generate_ai_reply(sender: str, subject: str, body: str):
    """Generate email reply using OpenAI"""
    system_msg = (
        "You are LiquidMail — an AI assistant. Write clear, helpful, professional replies."
    )

    user_msg = f"""
    From: {sender}
    Subject: {subject}

    {body}
    """

    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
    )

    return completion.choices[0].message.content

# -------------------------------------------------------
# ROUTES
# -------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "LiquidMail backend"}

# ------ OAuth: Begin login ------
@app.get("/gmail/connect")
def gmail_connect(user_email: str):
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/gmail.send",
        "state": user_email,
    }

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return {"oauth_url": url}

# ------ OAuth status ------
@app.get("/gmail/status")
def gmail_status(user_email: str):
    token = get_valid_access_token(user_email)
    return {"connected": token is not None}

# ------ OAuth callback ------
@app.get("/gmail/callback")
def gmail_callback(code: str, state: str):
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": GOOGLE_REDIRECT_URI
    })

    tokens = res.json()

    if "access_token" not in tokens:
        raise HTTPException(400, tokens)

    save_tokens(state, tokens)

    return {"success": True, "user_email": state}

# ------ AI Reply ------
@app.post("/ai/generate")
def ai_generate(req: GenerateReplyRequest):
    reply = generate_ai_reply(req.sender, req.subject, req.body)
    return {"reply": reply}

# ------ Send Email ------
@app.post("/gmail/send")
def gmail_send(req: SendEmailRequest):
    result = send_gmail(
        user_email=req.user_id,
        to_addr=req.to,
        subject=req.subject,
        body=req.body
    )
    return {"status": "sent", "details": result}

# -------------------------------------------------------
# ENTRY POINT (Railway)
# -------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
