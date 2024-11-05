from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from telethon import TelegramClient
import asyncio
from fastapi.middleware.cors import CORSMiddleware
import uuid

app = FastAPI()
clients = {}  # To store Telegram clients per phone number
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

# Helper function to create and store client
def create_client(api_id, api_hash, phone_number):
    client = TelegramClient(f'session_{phone_number}', api_id, api_hash)
    clients[phone_number] = client
    return client

@app.post("/start-auth/")
async def start_auth(credentials: Credentials):
    client = create_client(credentials.api_id, credentials.api_hash, credentials.phone_number)
    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(credentials.phone_number)
        return {
            "message": "Authorization required. Please enter the code using the /verify-code endpoint.",
            "data_sent": {
                "api_id": credentials.api_id,
                "api_hash": credentials.api_hash,
                "phone_number": credentials.phone_number
            }
        }

    return {
        "message": "Already authorized",
        "data_sent": {
            "api_id": credentials.api_id,
            "api_hash": credentials.api_hash,
            "phone_number": credentials.phone_number
        }
    }

@app.post("/verify-code/")
async def verify_code(verification: CodeVerification):
    client = clients.get(verification.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")

    try:
        # Attempt to sign in with the code
        await client.sign_in(verification.phone_number, verification.code)
        return {"message": "Authorization successful"}
    except Exception as e:
        if "PASSWORD" in str(e).upper():
            # Requires password for 2FA
            return {"message": "Password required. Use the /verify-password endpoint to complete authorization."}
        else:
            raise HTTPException(status_code=400, detail=f"Authorization failed: {str(e)}")

@app.post("/verify-password/")
async def verify_password(password_verification: PasswordVerification):
    client = clients.get(password_verification.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")

    try:
        # Sign in with password for 2FA
        await client.sign_in(password=password_verification.password)
        return {"message": "Authorization successful with 2FA"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authorization failed: {str(e)}")

@app.get("/list-chats")
async def list_chats(phone_number: str):
    client = clients.get(phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")
    
    try:
        if not client.is_connected():
            await client.connect()

        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="Client not authorized. Complete the authentication process.")

        dialogs = await client.get_dialogs()
        chats = [{"chat_id": dialog.id, "title": dialog.title} for dialog in dialogs]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")

    return {"chats": chats}

# Background task for message forwarding
async def forward_messages_to_channel(client, session_id, source_chat_id, destination_channel_ids, keywords):
    last_message_id = None

    try:
        while sessions.get(session_id):  # Chỉ tiếp tục nếu session_id vẫn còn hoạt động
            if not client.is_connected():  # Kiểm tra nếu client bị ngắt kết nối
                await client.connect()  # Kết nối lại nếu cần thiết

            messages = await client.get_messages(source_chat_id, limit=1)
            if last_message_id:
                messages = [msg for msg in messages if msg.id > last_message_id]

            for message in reversed(messages):
                if keywords:
                    keywords = [keyword.strip().lower() for keyword in keywords if keyword.strip()]
                    if message.text and any(keyword in message.text.lower() for keyword in keywords):
                        for destination_channel_id in destination_channel_ids:
                            if message.sticker:
                                await client.send_file(destination_channel_id, message.media)
                            elif message.photo or message.file:
                                await client.send_file(destination_channel_id, message.media)
                            else:
                                await client.send_message(destination_channel_id, message.text)
                else:
                    for destination_channel_id in destination_channel_ids:
                        if message.photo or message.file:
                            await client.send_file(destination_channel_id, message.media)
                        else:
                            await client.send_message(destination_channel_id, message.text)

                # Cập nhật last_message_id sau khi xử lý từng tin nhắn
                last_message_id = max(last_message_id or 0, message.id)

            await asyncio.sleep(5)
    except asyncio.CancelledError:
        print("Tác vụ đã bị hủy")
    except Exception as e:
        print(f"Đã xảy ra lỗi: {e}")
    finally:
        # Đảm bảo client ngắt kết nối khi kết thúc
        await client.disconnect()
        print("Client đã ngắt kết nối")

@app.post("/forward-messages/")
async def forward_messages(request: ForwardRequest, background_tasks: BackgroundTasks):
    client = clients.get(request.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")

    await client.connect()

    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Client not authorized. Complete the authentication process.")

    # Generate a unique session ID for this forwarding task
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"is_active": True, "source_chat_id": request.source_chat_id}

    # Run forward_messages_to_channel as a background task with unique session parameters
    background_tasks.add_task(
        forward_messages_to_channel,
        client,
        session_id,
        request.source_chat_id,
        request.destination_channel_ids,
        request.keywords
    )
    return {"message": "Forwarding process started in the background", "session_id": session_id}

@app.post("/stop-forwarding/")
async def stop_forwarding(session_id: str):
    if session_id in sessions:
        # Mark the session as inactive to stop the background task
        sessions.pop(session_id)
        return {"message": f"Forwarding process with session_id {session_id} has been stopped"}
    else:
        raise HTTPException(status_code=404, detail="Session not found")
    
@app.get("/get-sessions/")
async def get_sessions():
    active_sessions = [
        {"session_id": session_id, "source_chat_id": session["source_chat_id"]}
        for session_id, session in sessions.items() if isinstance(session, dict) and session.get("is_active", False)
    ]
    return {"active_sessions": active_sessions}