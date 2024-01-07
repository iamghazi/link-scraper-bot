from datetime import datetime, timedelta
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from contextlib import asynccontextmanager
from http import HTTPStatus
import os
import traceback 
import httpx
import requests
import json
from bs4 import BeautifulSoup
from fastapi_utilities import repeat_every
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.ext._contexttypes import ContextTypes
from dotenv import load_dotenv
from websockets.sync.client import connect
import cfscrape
import re
import asyncio
import time

load_dotenv()

global bot

TOKEN = os.getenv("BOT_TOKEN")  # Telegram Bot API Key
CHAT_ID = os.getenv("CHAT_ID")  # Telegram Chat ID
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # Webhook url

ETHER_HOST_URL = 'https://etherscan.io'
BSC_HOST_URL = 'https://bscscan.com'
PAGE_SIZE = 1
REFRESH_TIME_IN_SEC=10

MONGODB_URL = os.getenv("MONGODB_URL")  # Mongodb url
client = AsyncIOMotorClient(MONGODB_URL)
db = client.get_database("scans")
settings_collection = db.get_collection("settings")

ptb = (
    Application.builder()
    .updater(None)
    .token(TOKEN)
    .read_timeout(7)
    .get_updates_read_timeout(42)
    .build()
)

bsc_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"}

bsc_url = 'https://bscscan.com/contractsVerified'

ether_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-GB,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": "\"Brave\";v=\"119\", \"Chromium\";v=\"119\", \"Not?A_Brand\";v=\"24\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"macOS\"",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "sec-gpc": "1",
    "upgrade-insecure-requests": "1",
    "cookie": "ASP.NET_SessionId=jfknrjjfjk0m0k3snwuzts5h; etherscan_offset_datetime=+5.5; __cflb=02DiuFnsSsHWYH8WqVXcJWaecAw5gpnmeyh3ReXcWttUL; cf_clearance=AEUeXQH4vZNeExCzIVbi_dRGWi9DfKYVm0uHc93hNfc-1704665813-0-2-d38ff883.6f6c9269.70ad0cbf-0.2.1704665813",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
}

ether_url = 'https://etherscan.io/contractsVerified'

timeout = httpx.Timeout(10.0, connect=60.0)
limits = httpx.Limits(max_keepalive_connections=1000, max_connections=1000)

@asynccontextmanager
async def lifespan(_: FastAPI):
    await ptb.bot.setWebhook(WEBHOOK_URL)
    async with ptb:
        await ptb.start()
        await crawl_links_and_send_to_telegram()
        yield
        await ptb.stop()

app = FastAPI(lifespan=lifespan)

@app.get('/')
async def elb_check():
    return Response(status_code=HTTPStatus.OK)

@app.post("/")
async def process_update(request: Request):
    req = await request.json()
    update = Update.de_json(req, ptb.bot)
    await ptb.process_update(update)
    return Response(status_code=HTTPStatus.OK)

# Example handler
async def start(update, _: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command is issued."""
    try:
        text = update.message.text.encode('utf-8').decode().split(' ')

        if str(update.message.chat.id) != CHAT_ID: return
    
        command = text[0]
        print(command)
        text_message = None
        if command == "/start":
            await settings_collection.update_one({}, {'$set': {'ether_last_visited': ''}})
            text_message = "Welcome to EtherScan bot."
        # elif command == "/end":
        #     text_message = """
        #     Good bye!!
        #     """
        else:
            text_message = "Invalid command!"
        if text_message is not None: await update.message.reply_text(text_message)
        return 'ok'
    except Exception as e:
        traceback.print_exc()

ptb.add_handler(CommandHandler("start", start))

# Background processing
async def sendTgMessage(message):
    """
    Sends the Message to telegram with the Telegram BOT API
    """
    try:
        tg_msg = {"chat_id": CHAT_ID, "text": message, "parse_mode": "markdown", 'link_preview_options': { 'is_disabled': True }, }
        API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            await client.post(API_URL, json=tg_msg)
    except Exception as e:
        traceback.print_exc()


@app.on_event("startup")
@repeat_every(seconds=REFRESH_TIME_IN_SEC)
async def crawl_links_and_send_to_telegram():
    print('CONNECT')
    start = datetime.now()
    try:
        settings = await settings_collection.find_one()
        bsc_last_visited = settings['bsc_last_visited']
        ether_last_visited = settings['ether_last_visited']

        # bsc_links = await fetch_all_contracts(bsc_url)
        # print(bsc_links)

        ether_links = await fetch_all_contracts(ether_url, ether_last_visited)
        exec_async(ether_links, ETHER_HOST_URL)
        if len(ether_links): await settings_collection.update_one({}, { '$set': { 'ether_last_visited': ether_links[0] } })

    except Exception as e:
        traceback.print_exc()
    print(datetime.now() - start)

def exec_async(links, host_url):
    for link in links:
        asyncio.gather(*[asyncio.create_task(get_contract_details_and_send_to_telegram(link, host_url))])

async def fetch_all_contracts(url, last_visited = ''):
    all_links = []
    i = 0
    while True:
        if i == PAGE_SIZE: break
        try:

            links = await get_links(url)
            if links is None or len(links) == 0: break
            if last_visited in links:
                all_links.append(links[0:links.index(last_visited)])
                break
        except Exception as e:
            traceback.print_exc()
        i = i + 1
        all_links.append(links)
    return flatten(all_links)

async def get_links(url):
    try:
        # scraper = cfscrape.create_scraper(delay=10)
        get_res = requests.get(url, headers=ether_headers)
        print(get_res)

        if get_res.status_code != 200: return []
        soup = BeautifulSoup(get_res.content, 'html.parser')
        
        main_container = soup.find('div', { 'id': 'ContentPlaceHolder1_mainrow' })
        links = []
        for td in main_container.find_all('td'):
            anchor = td.find('a', { 'class': 'me-1' })
            if anchor and anchor.has_attr('href'): links.append(anchor['href'])

        return links
    except Exception as e:
        traceback.print_exc()

async def get_contract_details_and_send_to_telegram(href, base_url, base_name = 'Etherscan'):
    start = datetime.now()
    print('OK', href)
    url = f"{base_url}{href}"

    scraper = cfscrape.create_scraper()
    reqs = scraper.get(url=url)
    soup = BeautifulSoup(reqs.content, 'html.parser')


    contract_name = soup.find('div', {'id': 'ContentPlaceHolder1_contractCodeDiv'}).find('span', {'class': 'h6 fw-bold mb-0'}).encode_contents().decode('utf-8')
    contract_address = soup.find('span', {'id': 'mainaddress'}).encode_contents().decode('utf-8')
    
    info_urls = list(set(get_info_urls(soup, contract_name)))
    if len(info_urls) == 0: return
    
    info_urls.append(f'[{base_name}]({url})')

    message = f"""*{contract_name}*
{' | '.join(info_urls)}

{contract_address}"""
    await sendTgMessage(message)
    print(datetime.now() - start)

def get_info_urls(soup, contract_name):
    editor = soup.find('pre', {'id': 'editor'})
    if editor is None: return []

    editor = editor.encode_contents().decode('utf-8')



    URL_REGEX = r"""((?:(?:https|ftp|http)?:(?:/{1,3}|[a-z0-9%])|[a-z0-9.\-]+[.](?:com|org|uk)/)(?:[^\s()<>{}\[\]]+|\([^\s()]*?\([^\s()]+\)[^\s()]*?\)|\([^\s]+?\))+(?:\([^\s()]*?\([^\s()]+\)[^\s()]*?\)|\([^\s]+?\)|[^\s`!()\[\]{};:'".,<>?«»“”‘’])|(?:(?<!@)[a-z0-9]+(?:[.\-][a-z0-9]+)*[.](?:com|uk|ac)\b/?(?!@)))"""

    urls = re.findall(URL_REGEX, editor)
    urls = list(filter(lambda l: 'twitter.com' not in l and 'x.com' not in l and ('t.me' in l or contract_name.lower() in l.lower()), set(urls)))
    
    res = []
    for url in urls:
        res.append(f'[Telegram]({url})') if 't.me' in url else res.append(f'[Website]({url})')
    
    sorted(urls, key=lambda x: x[1])
    return res

def flatten(xss):
    return [x for xs in xss for x in xs]