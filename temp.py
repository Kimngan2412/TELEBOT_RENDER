from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from telethon import TelegramClient
import asyncio
import telethon.tl.types
from fastapi.middleware.cors import CORSMiddleware
import uuid
import os

app = FastAPI()
clients = {}  # Store Telegram clients per phone number
sessions = {}  # Dictionary to store active session configurations

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Credentials(BaseModel):
    api_id: int
    api_hash: str
    phone_number: str

class CodeVerification(BaseModel):
    phone_number: str
    code: str

class ForwardRequest(BaseModel):
    phone_number: str
    source_chat_id: int
    destination_channel_ids: list[int]
    keywords: list[str] = []

class PasswordVerification(BaseModel):
    phone_number: str
    password: str

def create_client(api_id, api_hash, phone_number):
    session_name = f"session_{phone_number}"
    client = TelegramClient(session_name, api_id, api_hash)
    clients[phone_number] = client
    return client

@app.post("/start-auth/")
async def start_auth(credentials: Credentials):
    client = clients.get(credentials.phone_number) or create_client(credentials.api_id, credentials.api_hash, credentials.phone_number)
    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(credentials.phone_number)
        return {"message": "Authorization required. Enter code via /verify-code endpoint."}

    return {"message": "Already authorized"}

@app.post("/verify-code/")
async def verify_code(verification: CodeVerification):
    client = clients.get(verification.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start auth process first.")
    try:
        await client.sign_in(verification.phone_number, verification.code)
        return {"message": "Authorization successful"}
    except Exception as e:
        if "PASSWORD" in str(e).upper():
            return {"message": "Password required. Use /verify-password endpoint."}
        raise HTTPException(status_code=400, detail=f"Authorization failed: {str(e)}")

@app.post("/verify-password/")
async def verify_password(password_verification: PasswordVerification):
    client = clients.get(password_verification.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found.")
    try:
        await client.sign_in(password=password_verification.password)
        return {"message": "Authorization successful with 2FA"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authorization failed: {str(e)}")

@app.get("/list-chats")
async def list_chats(phone_number: str):
    client = clients.get(phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found.")
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authorized.")
    dialogs = await client.get_dialogs()
    return {"chats": [{"chat_id": dialog.id, "title": dialog.title} for dialog in dialogs]}

async def forward_messages_to_channel(client, session_id, source_chat_id, destination_channel_ids, keywords):
    last_message_id = None
    try:
        while sessions.get(session_id):
            if not client.is_connected():
                await client.connect()
            messages = await client.get_messages(source_chat_id, limit=1)
            if last_message_id:
                messages = [msg for msg in messages if msg.id > last_message_id]
            for message in reversed(messages):
                content = message.text or ""
                if isinstance(message.media, telethon.tl.types.MessageMediaWebPage):
                    web_page = message.media.webpage
                    if web_page and hasattr(web_page, 'url'):
                        content += f"\n\nLink Preview: {web_page.url}"
                if keywords:
                    keywords = [kw.lower() for kw in keywords if kw]
                    if any(kw in content.lower() for kw in keywords):
                        for dest_id in destination_channel_ids:
                            if message.photo or message.file:
                                await client.send_file(dest_id, message.media, caption=content)
                            else:
                                await client.send_message(dest_id, content)
                else:
                    for dest_id in destination_channel_ids:
                        if message.photo or message.file:
                            await client.send_file(dest_id, message.media, caption=content)
                        else:
                            await client.send_message(dest_id, content)
                last_message_id = max(last_message_id or 0, message.id)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        print("Task cancelled")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.disconnect()
        print("Client disconnected")

@app.post("/forward-messages/")
async def forward_messages(request: ForwardRequest, background_tasks: BackgroundTasks):
    client = clients.get(request.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found.")
    await client.connect()
    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authorized.")
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"is_active": True}
    background_tasks.add_task(
        forward_messages_to_channel,
        client,
        session_id,
        request.source_chat_id,
        request.destination_channel_ids,
        request.keywords
    )
    return {"message": "Forwarding started", "session_id": session_id}

@app.post("/stop-forwarding")
async def stop_forwarding(session_id: str):
    if session_id in sessions:
        sessions.pop(session_id)
        return {"message": f"Stopped session {session_id}"}
    raise HTTPException(status_code=404, detail="Session not found")

@app.post("/logout")
async def logout(phone_number: str):
    client = clients.get(phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found.")
    try:
        await client.disconnect()
        clients.pop(phone_number, None)
        session_file = f'session_{phone_number}.session'
        if os.path.exists(session_file):
            os.remove(session_file)
        return {"message": "Logged out successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Logout error: {str(e)}")
