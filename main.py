import os
from bson import ObjectId
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from telethon import TelegramClient
import asyncio
import telethon.tl.types
from fastapi.middleware.cors import CORSMiddleware
import uuid
from motor.motor_asyncio import AsyncIOMotorClient

# FastAPI app setup
app = FastAPI()
clients = {}  # To store Telegram clients per phone number

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB setup
class MongoDB:
    def __init__(self, uri: str, db_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]

# Initialize MongoDB
mongodb = MongoDB(uri="mongodb+srv://msm-group:0809danh@cluster0.6fyes.mongodb.net", db_name="telegram_bot")

# Pydantic Models
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

# Helper functions
def create_client(api_id, api_hash, phone_number):
    session_name = f"session_{phone_number}"
    client = TelegramClient(session_name, api_id, api_hash)
    clients[phone_number] = client
    return client

MAX_CAPTION_LENGTH = 4096  # Telegram caption limit

async def save_user_info(phone_number, api_id, api_hash, session_id=None, chat_title=None):
    user_collection = mongodb.db["users"]
    await user_collection.update_one(
        {"phone_number": phone_number},
        {
            "$set": {
                "api_id": api_id,
                "api_hash": api_hash,
                "session_id": session_id,
                "chat_title": chat_title,
                "is_active": True
            }
        },
        upsert=True
    )

async def get_user_info(phone_number):
    user_collection = mongodb.db["users"]
    return await user_collection.find_one({"phone_number": phone_number})

async def ensure_connected(client):
    """
    Đảm bảo client luôn được kết nối.
    """
    if not client.is_connected():
        await client.connect()

async def maintain_connection(client):
    while True:
        try:
            await ensure_connected(client)  # Đảm bảo client luôn kết nối
            await asyncio.sleep(60)  # Ping mỗi 60 giây
        except Exception as e:
            print(f"Connection maintenance error: {e}")
            await asyncio.sleep(5)  # Chờ trước khi thử lại

@app.on_event("startup")
async def startup_event():
    for phone_number, client in clients.items():
        asyncio.create_task(maintain_connection(client))  # Khởi động maintain_connection

# FastAPI Endpoints
@app.post("/start-auth/")
async def start_auth(credentials: Credentials):
    client = clients.get(credentials.phone_number) or create_client(credentials.api_id, credentials.api_hash, credentials.phone_number)
    await client.connect()
    await ensure_connected(client)

    if not await client.is_user_authorized():
        await client.send_code_request(credentials.phone_number)
        await save_user_info(credentials.phone_number, credentials.api_id, credentials.api_hash)
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
        await client.sign_in(verification.phone_number, verification.code)
        return {"message": "Authorization successful"}
    except Exception as e:
        if "PASSWORD" in str(e).upper():
            return {"message": "Password required. Use the /verify-password endpoint to complete authorization."}
        else:
            raise HTTPException(status_code=400, detail=f"Authorization failed: {str(e)}")

@app.post("/verify-password/")
async def verify_password(password_verification: PasswordVerification):
    client = clients.get(password_verification.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")

    try:
        await client.sign_in(password=password_verification.password)
        return {"message": "Authorization successful with 2FA"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authorization failed: {str(e)}")
    
@app.get("/list-chats")
async def list_chats(phone_number: str):
    """
    API liệt kê danh sách các cuộc trò chuyện của người dùng
    """
    # Lấy thông tin người dùng từ MongoDB
    user_info = await get_user_info(phone_number)
    if not user_info:
        raise HTTPException(status_code=400, detail="User not found. Start the authorization process first.")

    client = clients.get(phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")
    
    try:
        # Đảm bảo client đã kết nối
        if not client.is_connected():
            await client.connect()

        # Kiểm tra xem người dùng đã được xác thực chưa
        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="Client not authorized. Complete the authentication process.")

        # Lấy danh sách các cuộc trò chuyện
        dialogs = await client.get_dialogs()
        chats = [{"chat_id": dialog.id, "title": dialog.title} for dialog in dialogs]

        return {"chats": chats}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
    

async def send_message_or_file(client, destination_channel_id, message, content):
    """
    Gửi tin nhắn hoặc file với kiểm tra loại media hợp lệ.
    """
    try:
        # Kiểm tra nếu media là MessageMediaWebPage
        if isinstance(message.media, telethon.tl.types.MessageMediaWebPage):
            # Chỉ gửi link preview dưới dạng tin nhắn văn bản
            web_page = message.media.webpage
            if web_page and hasattr(web_page, 'url'):
                content += f"\n\nLink Preview: {web_page.url}"
            await client.send_message(destination_channel_id, content)
        else:
            # Xử lý caption dài
            if len(content) > MAX_CAPTION_LENGTH:
                chunks = [content[i:i + MAX_CAPTION_LENGTH] for i in range(0, len(content), MAX_CAPTION_LENGTH)]
                if message.photo or message.file:
                    await client.send_file(destination_channel_id, message.media, caption=chunks[0])
                    for chunk in chunks[1:]:
                        await client.send_message(destination_channel_id, chunk)
                else:
                    for chunk in chunks:
                        await client.send_message(destination_channel_id, chunk)
            else:
                if message.photo or message.file:
                    await client.send_file(destination_channel_id, message.media, caption=content)
                else:
                    await client.send_message(destination_channel_id, content)
    except Exception as e:
        print(f"Error while sending message or file: {e}")


async def forward_messages_to_channel(client, session_id, source_chat_id, destination_channel_ids, keywords):
    """
    Forward messages from source_chat_id to destination_channel_ids with keyword filtering.
    """
    last_message_id = None  # Track the last processed message ID

    try:
        while True:
            # Ensure the client is connected
            await ensure_connected(client)

            # Check session status
            session_collection = mongodb.db["sessions"]
            session = await session_collection.find_one({"session_id": session_id, "is_active": True})
            if not session:
                print(f"Session {session_id} is no longer active. Stopping task.")
                break

            # Get the latest messages from the source
            messages = await client.get_messages(source_chat_id, limit=1)
            if last_message_id:
                messages = [msg for msg in messages if msg.id > last_message_id]

            for message in reversed(messages):
                try:
                    content = message.text or ""  # Get message content

                    # Handle MessageMediaWebPage
                    if isinstance(message.media, telethon.tl.types.MessageMediaWebPage):
                        web_page = message.media.webpage
                        if web_page and hasattr(web_page, 'url'):
                            content += f"\n\nLink Preview: {web_page.url}"

                    # Check keywords if provided
                    if keywords:
                        keywords = [keyword.strip().lower() for keyword in keywords if keyword.strip()]
                        if content and any(keyword in content.lower() for keyword in keywords):
                            for destination_channel_id in destination_channel_ids:
                                await send_message_or_file(client, destination_channel_id, message, content)
                    else:
                        # Forward all messages if no keywords
                        for destination_channel_id in destination_channel_ids:
                            await send_message_or_file(client, destination_channel_id, message, content)

                    # Update last_message_id
                    last_message_id = max(last_message_id or 0, message.id)
                except Exception as e:
                    print(f"Failed to forward message {message.id}: {e}")

            await asyncio.sleep(5)  # Wait before checking again
    except asyncio.CancelledError:
        print("Task was cancelled.")
    except Exception as e:
        print(f"An error occurred: {e}")
        await asyncio.sleep(5)  # Wait before retrying
    finally:
        print("Background task stopped.")
       
        
@app.post("/forward-messages/")
async def forward_messages(request: ForwardRequest, background_tasks: BackgroundTasks):
    """
    API to start forwarding messages from a source to destination channels.
    """
    # Retrieve user info from MongoDB
    user_collection = mongodb.db["users"]
    user_info = await user_collection.find_one({"phone_number": request.phone_number})
    if not user_info:
        raise HTTPException(status_code=400, detail="User not found. Start the authorization process first.")

    client = clients.get(request.phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found. Start the authorization process first.")

    # Ensure the client is authorized
    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Client not authorized. Complete the authentication process.")

    try:
        # Get chat title of the source
        chat = await client.get_entity(request.source_chat_id)
        chat_title = chat.title

        # Create session_id and save to MongoDB
        session_id = str(uuid.uuid4())
        session_collection = mongodb.db["sessions"]
        await session_collection.insert_one({
            "session_id": session_id,
            "phone_number": request.phone_number,
            "source_chat_id": request.source_chat_id,
            "destination_channel_ids": request.destination_channel_ids,
            "keywords": request.keywords,
            "is_active": True,
            "chat_title": chat_title
        })

        # Add forwarding task to background
        background_tasks.add_task(
            forward_messages_to_channel,
            client,
            session_id,
            request.source_chat_id,
            request.destination_channel_ids,
            request.keywords
        )

        return {"message": "Forwarding process started in the background", "session_id": session_id}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


@app.post("/stop-forwarding/")
async def stop_forwarding(session_id: str):
    user_collection = mongodb.db["sessions"]
    user = await user_collection.find_one({"session_id": session_id})
    if user:
        await user_collection.update_one({"session_id": session_id}, {"$set": {"is_active": False}})
        return {"message": f"Forwarding process with session_id {session_id} has been stopped"}
    else:
        raise HTTPException(status_code=404, detail="Session not found")

@app.get("/get-sessions")
async def get_sessions(phone_number: str):
    """
    API để lấy danh sách các session đang hoạt động của người dùng cụ thể từ MongoDB
    """
    session_collection = mongodb.db["sessions"]  # Đảm bảo truy vấn đúng collection
    
    # Truy vấn các session của số điện thoại hiện tại
    sessions = await session_collection.find({"is_active": True, "phone_number": phone_number}).to_list(length=None)

    # Chuyển đổi ObjectId và trả về dữ liệu chính xác
    def convert_objectid_to_str(document):
        if isinstance(document, dict):
            return {k: (str(v) if isinstance(v, ObjectId) else v) for k, v in document.items()}
        elif isinstance(document, list):
            return [convert_objectid_to_str(item) for item in document]
        else:
            return document

    sessions = convert_objectid_to_str(sessions)

    return {"active_sessions": sessions}




@app.post("/logout")
async def logout(phone_number: str):
    """
    API để đăng xuất người dùng và xóa thông tin khỏi MongoDB
    """
    # Lấy thông tin người dùng từ MongoDB
    user_info = await get_user_info(phone_number)
    if not user_info:
        raise HTTPException(status_code=400, detail="User not found or already logged out.")

    client = clients.get(phone_number)
    if not client:
        raise HTTPException(status_code=400, detail="Client not found or already logged out.")

    try:
        # Ngắt kết nối Telegram Client
        await client.disconnect()

        # Xóa thông tin người dùng khỏi MongoDB
        user_collection = mongodb.db["users"]
        await user_collection.delete_one({"phone_number": phone_number})

        # Xóa client khỏi bộ nhớ
        clients.pop(phone_number, None)

        # Xóa file session nếu tồn tại
        session_file = f"session_{phone_number}.session"
        if os.path.exists(session_file):
            os.remove(session_file)
            
        session_collection = mongodb.db["sessions"]
        await session_collection.delete_many({"phone_number": phone_number})

        return {"message": f"Successfully logged out for phone number {phone_number}."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during logout: {str(e)}")
