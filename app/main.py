import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

import threading
from contextlib import asynccontextmanager
from app.outbox import run_relay


from app.exceptions import (
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.middleware import add_process_time_header, log_requests
from app.routers import cache, tasks
#logging ka common format set karti hai. Middleware aur exception handlers messages bhejte hain. Logging system un messages ko configured format mein terminal par dikhata hai.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",

)
#thread:Ek separate path of execution jo main program ke saath independently kaam karta hai
# lifespan: app start hone se pehle aur band hone ke baad ka code yahan chalta h
# yield se pehle = startup, yield ke baad = shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()#event ekk signal hota h jo ekk thread s dosre thread ko stop hone ka msg data h
    # relay ko alag thread mein chalate hain taake API ke requests block na hon
    # daemon=True: agar app achanak band ho jaye to ye thread bhi saath band ho jaye, atka na rahe
    thread = threading.Thread(target=run_relay, args=(stop,), daemon=True)
    thread.start()
    yield  # yahan app chal rahi hoti h aur requests aa rahi hoti hain
    # app band ho rahi h: thread ko bolo ruk jao, phir max 5 second uska intezar karo
    stop.set()
    thread.join(timeout=5)


# lifespan yahan pass kiya taake FastAPI start/stop par upar wala function chalaye
app = FastAPI(title="Task Manager API", lifespan=lifespan)


# Last registered is outermost: timing runs inside logging so logging can read its header
app.middleware("http")(add_process_time_header)
app.middleware("http")(log_requests)

# routers: har file ke routes (tasks, cache) main app mein jod diye
app.include_router(tasks.router)
app.include_router(cache.router)

# exception handlers: error aaye to har jagah same format ka response jaye
# HTTPException = jaise 404, RequestValidationError = galat input (422), Exception = baqi sab (500)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)
