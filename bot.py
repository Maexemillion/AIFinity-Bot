import os, json, asyncio, hashlib, datetime
import aiohttp
import feedparser
from bs4 import BeautifulSoup
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "0"))
FUT_CHANNEL_ID  = int(os.getenv("FUT_CHANNEL_ID", "0"))
STATE_FILE = "news_state.json"
UA_HEADERS = {"User-Agent": "AIFinityHubBot/1.0 (+https://discord.gg/)"}

state = json.load(open(STATE_FILE, "r", encoding="utf-8")) if os.path.exists(STATE_FILE) else {}

def seen(uid: str) -> bool: return uid in state
def mark(uid: str): state[uid] = int(datetime.datetime.utcnow().timestamp())
def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def trim(s, n): 
    if not s: return ""
    return s if len(s) <= n else s[:n-3]+"..."

async def send_embed(ch: discord.TextChannel, title, url, desc, footer):
    emb = discord.Embed(
        title=trim(title or "Update", 256),
        url=url or discord.Embed.Empty,
        description=trim(desc or "", 4000),
        timestamp=datetime.datetime.utcnow(),
        color=discord.Color.blurple()
    )
    emb.set_footer(text=footer)
    await ch.send(embed=emb)

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=UA_HEADERS, timeout=aiohttp.ClientTimeout(total=25)) as r:
        r.raise_for_status()
        return await r.text()

async def fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers=UA_HEADERS, timeout=aiohttp.ClientTimeout(total=25)) as r:
        r.raise_for_status()
        return await r.json()

async def do_hf_blog(session, ch):
    feed_url = "https://huggingface.co/blog/feed.xml"
    # feedparser arbeitet mit Text
    xml = await fetch_text(session, feed_url)
    d = feedparser.parse(xml)
    pushed = 0
    for e in d.entries[:10]:
        uid_seed = (e.get("id") or e.get("link") or e.get("title","")) + "HF"
        uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:16]
        if seen(uid): continue
        title = e.get("title","HF News"); link=e.get("link","")
        desc = e.get("summary","") or e.get("description","")
        await send_embed(ch, title, link, desc, "AIFinity Hub • AI News")
        mark(uid); pushed += 1; await asyncio.sleep(0.7)
    return pushed

async def do_civitai_api(session, ch):
    url = "https://civitai.com/api/v1/models?limit=5&sort=Newest"
    data = await fetch_json(session, url)
    pushed = 0
    for m in data.get("items", []):
        title = m.get("name","CivitAI Model")
        link  = f"https://civitai.com/models/{m.get('id')}"
        desc  = m.get("description") or ""
        uid   = hashlib.sha256(f"CIVITAI:{m.get('id')}".encode()).hexdigest()[:16]
        if seen(uid): continue
        await send_embed(ch, f"🚀 New Model: {title}", link, desc, "AIFinity Hub • AI News")
        mark(uid); pushed += 1; await asyncio.sleep(0.7)
    return pushed

async def do_ea_press(session, ch):
    feed_url = "https://news.ea.com/rss/pressrelease.aspx"
    xml = await fetch_text(session, feed_url)
    d = feedparser.parse(xml)
    pushed = 0
    for e in d.entries[:10]:
        uid_seed = (e.get("id") or e.get("link") or e.get("title","")) + "EA"
        uid = hashlib.sha256(uid_seed.encode()).hexdigest()[:16]
        if seen(uid): continue
        title = e.get("title","EA News"); link=e.get("link","")
        desc = e.get("summary","") or e.get("description","")
        await send_embed(ch, f"EA Update: {title}", link, desc, "AIFinity Hub • FUT News")
        mark(uid); pushed += 1; await asyncio.sleep(0.7)
    return pushed

async def do_futgg(session, ch):
    url = "https://www.fut.gg/news/"
    html = await fetch_text(session, url)
    soup = BeautifulSoup(html, "html.parser")
    posts = soup.select("a[href^='/news/']")[:8]
    pushed = 0
    seen_links = set()
    for a in posts:
        href = a.get("href","")
        if not href or href in seen_links: continue
        seen_links.add(href)
        link = "https://www.fut.gg" + href
        title = a.get_text(strip=True) or "FUT.GG News"
        uid   = hashlib.sha256(f"FUTGG:{href}".encode()).hexdigest()[:16]
        if seen(uid): continue
        await send_embed(ch, f"FUT.GG: {title}", link, "", "AIFinity Hub • FUT News")
        mark(uid); pushed += 1; await asyncio.sleep(0.7)
    return pushed

class NewsClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.job_task = None

    async def setup_hook(self):
        if GUILD_ID:
            self.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
            await self.tree.sync(guild=discord.Object(id=GUILD_ID))
        else:
            await self.tree.sync()

        @self.tree.command(name="news_test", description="Poste eine Testnachricht")
        async def news_test(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            ch = self.get_channel(NEWS_CHANNEL_ID)
            await send_embed(ch, "✅ Test", "", "Das ist ein Test-Embed.", "AIFinity Hub • AI News")
            await interaction.followup.send("Test gesendet.", ephemeral=True)

        @self.tree.command(name="news_run", description="Jetzt Feeds abrufen & posten")
        async def news_run(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            pushed = await run_once(self)
            await interaction.followup.send(f"Fertig. Neue Posts: {pushed}", ephemeral=True)

        # Hintergrundjob starten
        self.job_task = asyncio.create_task(job_loop(self))

async def run_once(client: discord.Client) -> int:
    pushed = 0
    async with aiohttp.ClientSession() as session:
        if NEWS_CHANNEL_ID:
            ch = client.get_channel(NEWS_CHANNEL_ID)
            if ch: 
                pushed += await do_hf_blog(session, ch)
                pushed += await do_civitai_api(session, ch)
        if FUT_CHANNEL_ID:
            ch2 = client.get_channel(FUT_CHANNEL_ID)
            if ch2:
                pushed += await do_ea_press(session, ch2)
                pushed += await do_futgg(session, ch2)
    save_state()
    return pushed

async def job_loop(client: discord.Client):
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await run_once(client)
        except Exception as e:
            print("Job Fehler:", e)
        await asyncio.sleep(20 * 60)  # alle 20 Minuten

if __name__ == "__main__":
    NewsClient().run(TOKEN)
