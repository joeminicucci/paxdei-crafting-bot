import discord
from discord.ext import commands
from discord import app_commands
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from math import ceil
import re
import os
import io
from functools import lru_cache
import asyncio

# ------------------- CONFIG -------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in environment variables!")

BASE_URL = "https://paxdei.gaming.tools"
HEADERS = {
    "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ------------------- CACHING -------------------
@lru_cache(maxsize=128)
def find_recipe_url(item_name: str) -> str | None:
    try:
        ddgs = DDGS()
        results = ddgs.text(
            f'site:paxdei.gaming.tools intitle:"Pax Dei Recipe: {item_name}"',
            max_results=1,
            timeout=5,
        )
        if results:
            return results[0]["href"]
    except Exception as e:
        print(f"[Search] {e}")
    return None


@lru_cache(maxsize=128)
def scrape_recipe(recipe_url: str):
    try:
        r = requests.get(recipe_url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text()

        # Skill & Difficulty
        m = re.search(r"Skill:\s*([^<>\n]+?)\s*Difficulty:\s*(\d+)", text, re.I)
        if not m:
            return None
        skill, diff = m.group(1).strip(), int(m.group(2))

        # Name
        name = (
            soup.find("h1").get_text().replace("Pax Dei Recipe: ", "").strip()
            if soup.find("h1")
            else ""
        )

        # Ingredients
        ingredients = {}
        for a in soup.find_all("a", href=re.compile(r"/items/|/recipes/")):
            parent = a.find_parent("strong") or a.find_parent("p")
            if parent:
                line = parent.get_text().strip()
                qty_match = re.match(r"(.+?)\s*x\s*(\d+)", line, re.I)
                if qty_match:
                    sub_name = qty_match.group(1).strip()
                    sub_qty = int(qty_match.group(2))
                    href = a["href"]
                    link = BASE_URL + href if href.startswith("/") else href
                    slug = href.split("/")[-1]
                    ingredients[sub_name] = {
                        "qty_per": sub_qty,
                        "slug": slug,
                        "link": link,
                    }

        # Yield
        yield_per = 1
        y = re.search(r"yields?\s*(\d+)", text, re.I)
        if y:
            yield_per = int(y.group(1))

        return {
            "name": name,
            "skill": skill,
            "diff": diff,
            "ingredients": ingredients,
            "yield_per": yield_per,
            "url": recipe_url,
        }
    except Exception as e:
        print(f"[Scrape] {e}")
        return None


@lru_cache(maxsize=128)
def get_max_stack(slug: str) -> int:
    try:
        r = requests.get(f"{BASE_URL}/items/{slug}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        m = re.search(r"Max Stack[:\s]*(\d+)", BeautifulSoup(r.text, "html.parser").get_text(), re.I)
        if m:
            return int(m.group(1))
    except:
        pass
    return 50


# ------------------- FAILURE LOGIC -------------------
def get_fail_multiplier(diff: int, adjusted_level: int) -> float:
    delta = diff - adjusted_level
    if delta <= 0:
        return 1 / (1 - 0.08)   # Very Easy 8%
    if delta <= 3:
        return 1 / (1 - 0.15)   # Easy 15%
    if delta <= 6:
        return 1 / (1 - 0.30)   # Moderate 30%
    return 1 / (1 - 0.50)       # Hard 50%


# ------------------- RECURSIVE RAW CALC -------------------
def compute_raw(
    name: str,
    slug: str,
    needed_qty: float,
    level: int,
    apply_fail: bool = True,
):
    url = find_recipe_url(name)
    if not url:
        return {slug: needed_qty}

    recipe = scrape_recipe(url)
    if not recipe:
        return {slug: needed_qty}

    adj_lvl = level + 1 if apply_fail else 999
    mult = get_fail_multiplier(recipe["diff"], adj_lvl) if apply_fail else 1.0
    crafts = ceil(needed_qty / recipe["yield_per"]) * mult

    raw = {}
    for sub_name, info in recipe["ingredients"].items():
        sub_needed = info["qty_per"] * crafts
        sub_raw = compute_raw(sub_name, info["slug"], sub_needed, level, apply_fail)
        for r_slug, q in sub_raw.items():
            raw[r_slug] = raw.get(r_slug, 0) + q
    return raw


# ------------------- BREAKDOWN BUILDER -------------------
def build_breakdown(name: str, needed_qty: float, level: int, stacks_cache: dict):
    url = find_recipe_url(name)
    if not url:
        return ""
    recipe = scrape_recipe(url)
    if not recipe:
        return ""

    crafts = ceil(needed_qty / recipe["yield_per"])
    bd = f"### 1. [{recipe['name']}]({recipe['url']})\n"
    bd += f"- **Needed**: `{int(needed_qty)}`\n"
    inputs = ", ".join(
        f"{info['qty_per']}x {sub}" for sub, info in recipe["ingredients"].items()
    )
    bd += f"- **Batch Craft**: `{inputs} → {recipe['yield_per']}x {recipe['name']}`\n"
    bd += f"- **Crafts Required**: `ceil({needed_qty} / {recipe['yield_per']}) = {crafts}`\n\n"

    table = "| Raw Resource | Qty | Max Stack | Slots | Method | Link |\n"
    table += "|--------------|-----|-----------|-------|--------|------|\n"
    for sub_name, info in sorted(recipe["ingredients"].items(), key=lambda x: x[0]):
        sub_needed = info["qty_per"] * crafts
        max_st = stacks_cache.get(info["slug"], get_max_stack(info["slug"]))
        slots = ceil(sub_needed / max_st)
        method = "gather" if not find_recipe_url(sub_name) else "craft"
        table += (
            f"| [{sub_name}]({info['link']}) | `{int(sub_needed)}` | {max_st} "
            f"| `{slots}` | {method} | [{sub_name}]({info['link']}) |\n"
        )
        bd += build_breakdown(sub_name, sub_needed, level, stacks_cache)
        bd += "\n**Subtotal/Bonus Notes**\n\n"
    bd += table + "\n**Subtotal/Bonus Notes**\n\n"
    return bd


# ------------------- MAIN GENERATOR -------------------
async def generate_breakdown(item_name: str, quantity: int, level: int):
    # original (no fail) & adjusted (with fail buffer)
    orig = compute_raw(item_name, "", quantity, level, False)
    adj = compute_raw(item_name, "", quantity, level, True)

    stacks = {s: get_max_stack(s) for s in set(orig) | set(adj)}

    breakdown = build_breakdown(item_name, quantity, level, stacks)

    slots_orig = sum(ceil(q / stacks.get(s, 50)) for s, q in orig.items())
    slots_adj = sum(ceil(q / stacks.get(s, 50)) for s, q in adj.items())
    chests_adj = ceil(slots_adj / 20)

    final_tbl = "| Raw Resource | Qty (Orig) | Qty (Adj) | Slots (Adj) |\n"
    final_tbl += "|-----|------------|-----------|-------------|\n"
    for slug in sorted(set(orig) | set(adj)):
        qo = int(orig.get(slug, 0))
        qa = int(adj.get(slug, 0))
        sa = ceil(qa / stacks.get(slug, 50))
        name = slug.replace("_", " ").title()
        final_tbl += f"| [{name}]({BASE_URL}/items/{slug}) | `{qo}` | `{qa}` | `{sa}` |\n"
    pct = int((slots_adj / slots_orig - 1) * 100) if slots_orig else 0
    final_tbl += f"**Total Adj: {slots_adj} slots** (+{pct}%)\n"

    md = f"""**{item_name.title()} – Full Recursive Breakdown for {quantity}x (Level {level} +1 Blessing)**

## Step-by-Step Batch & Stack Calculation
{breakdown}

## Final Gather Totals & Storage Needs
{final_tbl}

**Total Raw Slots**: `{slots_orig}` → **~{ceil(slots_orig / 20)} Chests**

> **Bonus**: Adjusted for failure rates (Very Easy:8%, Easy:15%, Moderate:30%, Hard:50%)  
> **All math in `code`**  
> **Verified from paxdei.gaming.tools**
"""
    chat = breakdown[:1900] + "..." if len(breakdown) > 1900 else breakdown
    return md, chat


# ------------------- BOT -------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.tree.command(name="craft", description="Full Pax Dei crafting breakdown")
@app_commands.describe(
    item="Exact item name (e.g. Staff of Divine II)",
    quantity="How many to craft",
    level="Your skill level (blessing adds +1)",
)
async def craft(
    interaction: discord.Interaction,
    item: str,
    quantity: int,
    level: int,
):
    """All three parameters are **required**."""
    await interaction.response.defer()
    try:
        md_full, md_chat = await generate_breakdown(item, quantity, level)
        embed = discord.Embed(
            title=f"{item.title()} – {quantity}x (Lvl {level})",
            description=md_chat,
            color=0x00ff00,
        )
        await interaction.followup.send(embed=embed)
        file = discord.File(
            io.StringIO(md_full),
            filename=f"{item.replace(' ', '_')}_{quantity}x_breakdown.md",
        )
        await interaction.followup.send("### Full .md File", file=file)
    except Exception as e:
        await interaction.followup.send(f"**Error:** `{e}`")


@bot.event
async def on_ready():
    print(f"Bot online → {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Sync failed: {e}")


# ------------------- START -------------------
if __name__ == "__main__":
    bot.run(TOKEN)
