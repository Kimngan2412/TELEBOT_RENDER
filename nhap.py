from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from telethon import TelegramClient
import asyncio
import telethon.tl.types
from fastapi.middleware.cors import CORSMiddleware
import uuid
import os

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
# Hàm hỗ trợ tạo và lưu trữ client duy nhất cho mỗi số điện thoại
def create_client(api_id, api_hash, phone_number):
    # Đảm bảo mỗi file phiên là duy nhất cho mỗi số điện thoại
    session_name = f"session_{phone_number}"
    client = TelegramClient(session_name, api_id, api_hash)
    clients[phone_number] = client
    return client

@app.post("/start-auth/")
async def start_auth(credentials: Credentials):
    # Tạo hoặc lấy client cho số điện thoại này
    client = clients.get(credentials.phone_number) or create_client(credentials.api_id, credentials.api_hash, credentials.phone_number)
    await client.connect()

    # Kiểm tra xem client đã được xác thực chưa
    if not await client.is_user_authorized():
        # Gửi mã xác thực
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
        while sessions.get(session_id):  # Continue only if session_id is active
            if not client.is_connected():  # Reconnect if client is disconnected
                await client.connect()

            messages = await client.get_messages(source_chat_id, limit=1)
            if last_message_id:
                messages = [msg for msg in messages if msg.id > last_message_id]

            for message in reversed(messages):
                # Initialize content
                content = message.text or ""
                
                # Handle MessageMediaWebPage separately
                if isinstance(message.media, telethon.tl.types.MessageMediaWebPage):
                    web_page = message.media.webpage
                    # Add the web page URL if available
                    if web_page and hasattr(web_page, 'url'):
                        content += f"\n\nLink Preview: {web_page.url}"

                # Check for keywords if specified
                if keywords:
                    keywords = [keyword.strip().lower() for keyword in keywords if keyword.strip()]
                    if content and any(keyword in content.lower() for keyword in keywords):
                        for destination_channel_id in destination_channel_ids:
                            # Send media with caption if both exist
                            if message.photo or message.file:
                                await client.send_file(destination_channel_id, message.media, caption=content)
                            else:
                                await client.send_message(destination_channel_id, content)
                else:
                    # Forward message without filtering
                    for destination_channel_id in destination_channel_ids:
                        # Send media with caption if both exist
                        if message.photo or message.file:
                            await client.send_file(destination_channel_id, message.media, caption=content)
                        else:
                            await client.send_message(destination_channel_id, content)

                # Update last_message_id after processing each message
                last_message_id = max(last_message_id or 0, message.id)

            await asyncio.sleep(5)
    except asyncio.CancelledError:
        print("Task was cancelled")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Ensure the client disconnects at the end
        await client.disconnect()
        print("Client has disconnected")

@app.post("/forward-messages/")
async def forward_messages(request: ForwardRequest, background_tasks: BackgroundTasks):
    client = clients.get(request.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")

    await client.connect()

    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Client not authorized. Complete the authentication process.")

    # Retrieve the chat title for the source chat
    try:
        chat = await client.get_entity(request.source_chat_id)
        chat_title = chat.title
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve chat title: {str(e)}")

    # Generate a unique session ID for this forwarding task
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"is_active": True, "chat_title": chat_title}

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

@app.post("/stop-forwarding")
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
        {"session_id": session_id, "chat_title": session["chat_title"]}
        for session_id, session in sessions.items() if isinstance(session, dict) and session.get("is_active", False)
    ]
    return {"active_sessions": active_sessions}

@app.post("/logout")
async def logout(phone_number: str):
    # Check if the client exists
    client = clients.get(phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found or already logged out.")

    try:
        # Disconnect the client
        await client.disconnect()
        
        # Remove the client from the clients and sessions dictionaries
        clients.pop(phone_number, None)
        session_to_remove = [sid for sid, session in sessions.items() if session.get("source_chat_id") == phone_number]
        
        for sid in session_to_remove:
            sessions.pop(sid, None)

        # Remove the session file
        session_file = f'session_{phone_number}.session'
        if os.path.exists(session_file):
            os.remove(session_file)
        
        return {"message": "Successfully logged out and session data cleared."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during logout: {str(e)}")