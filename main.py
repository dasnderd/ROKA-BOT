import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import re
import asyncio
import aiohttp
from datetime import datetime

TOKEN = os.getenv("DISCORD_BOT")
ORG_CHANNEL_ID = int(os.getenv("ORG_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EMBED_CONFIG_PATH = os.path.join(BASE_DIR, "embed_config.json")
MESSAGE_IDS_PATH = os.path.join(BASE_DIR, "message_ids.json")
APPOINTMENTS_PATH = os.path.join(BASE_DIR, "appointments.json")
HISTORY_PATH = os.path.join(BASE_DIR, "appointment_history.json")

# -----------------------------------------------------------------------------
# 계급·권한 설정
# -----------------------------------------------------------------------------

STAR_RANKS: dict[str, int] = {
    "대장": 4,
    "중장": 3,
    "소장": 2,
    "준장": 1,
}

HEAD_ADMIN_KEYWORDS = ["국방부", "HeadAdmin"]
GENERAL_OFFICER_ROLE = "장성급 장교"
RESTRICTED_UNITS = ["군 수뇌부", "국방부"]

# -----------------------------------------------------------------------------
# 전역 변수 및 데이터 로드/세이브 함수
# -----------------------------------------------------------------------------

BOT_OWNER_ID: int = 0
POSITIONS_BY_UNIT: dict[str, list[str]] = {}

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] {path} 로드 실패: {e}")
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[ERROR] {path} 저장 실패: {e}")

embed_config = load_json(EMBED_CONFIG_PATH, {})
message_ids = load_json(MESSAGE_IDS_PATH, {})
appointments = load_json(APPOINTMENTS_PATH, {})
appointment_history = load_json(HISTORY_PATH, [])

# -----------------------------------------------------------------------------
# 헬퍼 함수
# -----------------------------------------------------------------------------

def is_head_admin(member: discord.Member) -> bool:
    if member.id == BOT_OWNER_ID:
        return True
    if member.guild_permissions.administrator:
        return True
    for role in member.roles:
        if any(kw in role.name for kw in HEAD_ADMIN_KEYWORDS):
            return True
    return False

def get_star_rank(member: discord.Member) -> int:
    max_rank = 0
    for role in member.roles:
        if role.name in STAR_RANKS:
            max_rank = max(max_rank, STAR_RANKS[role.name])
    return max_rank

def has_general_role(member: discord.Member) -> bool:
    return any(role.name == GENERAL_OFFICER_ROLE for role in member.roles)

def can_manage_target(operator: discord.Member, target_unit: str, target_position: str) -> bool:
    if is_head_admin(operator):
        return True
    
    if target_unit in RESTRICTED_UNITS:
        return False
        
    op_rank = get_star_rank(operator)
    if op_rank >= 3:
        return True
    elif op_rank >= 1:
        if has_general_role(operator):
            return True
            
    return False

def clean_markdown(text: str) -> str:
    if not text:
        return text
    return re.sub(r"[*_`~|>]", "", text)

# -----------------------------------------------------------------------------
# 디스코드 봇 설정 (인텐트 및 클라이언트)
# -----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False

    async def setup_hook(self):
        pass

bot = MyBot()

# -----------------------------------------------------------------------------
# 임베드 생성 및 업데이트 핵심 로직
# -----------------------------------------------------------------------------

def create_org_embed(unit_name: str, config: dict, current_appointments: dict) -> discord.Embed:
    embed = discord.Embed(
        title=config.get("title", f"🏛️ {unit_name} 직제 현황판"),
        description=config.get("description", "하단의 직제표를 확인해 주시기 바랍니다."),
        color=int(config.get("color", "0x00ff00").replace("0x", ""), 16)
    )
    
    if "thumbnail" in config and config["thumbnail"].startswith("http"):
        embed.set_thumbnail(url=config["thumbnail"])
    if "image" in config and config["image"].startswith("http"):
        embed.set_image(url=config["image"])
        
    embed.set_footer(text=config.get("footer", "ROKA-A Ministry of National Defense"))
    embed.timestamp = datetime.utcnow()

    positions = POSITIONS_BY_UNIT.get(unit_name, [])
    unit_apps = current_appointments.get(unit_name, {})

    for pos in positions:
        user_id_str = unit_apps.get(pos)
        if user_id_str:
            value_text = f"<@{user_id_str}>"
        else:
            value_text = "🔴 **공석 (Vacant)**"
            
        embed.add_field(name=f"▫️ {pos}", value=value_text, inline=False)

    return embed

async def update_all_embeds(guild: discord.Guild):
    global message_ids, appointments
    channel = guild.get_channel(ORG_CHANNEL_ID)
    if not channel:
        print(f"[WARN] 출력 채널({ORG_CHANNEL_ID})을 찾을 수 없습니다.")
        return

    saved_msg_ids = message_ids.get(str(guild.id), {})
    new_msg_ids = {}

    for unit_name in POSITIONS_BY_UNIT.keys():
        config = embed_config.get(unit_name, {})
        embed = create_org_embed(unit_name, config, appointments)
        
        msg_id = saved_msg_ids.get(unit_name)
        msg = None
        
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
            except discord.NotFound:
                msg = None
            except Exception as e:
                print(f"[ERROR] 메시지 패치 실패 ({unit_name}): {e}")
                msg = None

        if msg:
            try:
                await msg.edit(embed=embed)
                new_msg_ids[unit_name] = str(msg.id)
            except Exception as e:
                print(f"[ERROR] 메시지 수정 실패 ({unit_name}): {e}")
                try:
                    new_msg = await channel.send(embed=embed)
                    new_msg_ids[unit_name] = str(new_msg.id)
                except Exception as se:
                    print(f"[CRITICAL] 메시지 재전송 실패: {se}")
        else:
            try:
                new_msg = await channel.send(embed=embed)
                new_msg_ids[unit_name] = str(new_msg.id)
            except Exception as e:
                print(f"[ERROR] 메시지 신규 전송 실패 ({unit_name}): {e}")

    message_ids[str(guild.id)] = new_msg_ids
    save_json(MESSAGE_IDS_PATH, message_ids)

async def log_action(guild: discord.Guild, embed: discord.Embed):
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"[ERROR] 로그 전송 실패: {e}")

# -----------------------------------------------------------------------------
# 슬래시 명령어 (Slash Commands)
# -----------------------------------------------------------------------------

@bot.tree.command(name="초기화", description="[관리자] 설정된 데이터를 기반으로 현황판 메시지를 새로 생성하거나 동기화합니다.")
async def cmd_init(interaction: discord.Interaction):
    if not is_head_admin(interaction.user):
        await interaction.response.send_answer("❌ 이 명령어를 사용할 권한이 없습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await update_all_embeds(interaction.guild)
    await interaction.followup.send("✅ 직제 현황판 초기화 및 업데이트가 완료되었습니다.")

@bot.tree.command(name="임명", description="[간부] 특정 부서의 보직에 유저를 임명합니다.")
@app_commands.describe(부서="임명할 부서 이름", 보직="임명할 보직 이름", 대상="임명할 대상 유저")
async def cmd_appoint(interaction: discord.Interaction, 부서: str, 보직: str, 대상: discord.User):
    operator = interaction.user
    
    if 부서 not in POSITIONS_BY_UNIT:
        await interaction.response.send_message(f"❌ '{부서}'은(는) 등록되지 않은 부서입니다.", ephemeral=True)
        return
    if 보직 not in POSITIONS_BY_UNIT[부서]:
        await interaction.response.send_message(f"❌ '{보직}'은(는) '{부서}' 산하에 없는 보직입니다.", ephemeral=True)
        return

    if not can_manage_target(operator, 부서, 보직):
        await interaction.response.send_message("❌ 해당 보직을 관리할 권한이 없거나, 하위 장성이 관리할 수 없는 제한된 부서입니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if 부서 not in appointments:
        appointments[부서] = {}

    old_user_id = appointments[부서].get(보직)
    appointments[부서][보직] = str(대상.id)
    save_json(APPOINTMENTS_PATH, appointments)

    log_embed = discord.Embed(title="🎖️ 인사 임명 발령", color=0x00ff00, timestamp=datetime.utcnow())
    log_embed.add_field(name="발령 부서 및 보직", value=f"**{부서} — {보직}**", inline=False)
    log_embed.add_field(name="임명 대상자", value=f"<@{대상.id}> ({clean_markdown(대상.name)})", inline=True)
    if old_user_id:
        log_embed.add_field(name="이전 보직자", value=f"<@{old_user_id}>", inline=True)
    else:
        log_embed.add_field(name="이전 보직자", value="공석", inline=True)
    log_embed.add_field(name="명령권자", value=f"<@{operator.id}>", inline=False)
    log_embed.set_footer(text="ROKA-A Personnel Department")

    await log_action(interaction.guild, log_embed)
    await update_all_embeds(interaction.guild)
    await interaction.followup.send(f"✅ {부서}의 **{보직}** 자리에 <@{대상.id}>님을 성공적으로 임명했습니다.")

@bot.tree.command(name="해임", description="[간부] 특정 부서의 보직에 있는 유저를 해임하여 공석으로 만듭니다.")
@app_commands.describe(부서="해임할 보직이 속한 부서 이름", 보직="해임할 보직 이름")
async def cmd_dismiss(interaction: discord.Interaction, 부서: str, 보직: str):
    operator = interaction.user

    if 부서 not in appointments or 보직 not in appointments[부서]:
        await interaction.response.send_message("❌ 해당 보직은 이미 공석이거나 유저가 임명되어 있지 않습니다.", ephemeral=True)
        return

    if not can_manage_target(operator, 부서, 보직):
        await interaction.response.send_message("❌ 해당 보직을 해임할 권한이 없거나, 제한된 부서입니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    old_user_id = appointments[부서].pop(보직, None)
    save_json(APPOINTMENTS_PATH, appointments)

    log_embed = discord.Embed(title="🛑 인사 해임 발령", color=0xff0000, timestamp=datetime.utcnow())
    log_embed.add_field(name="해임 부서 및 보직", value=f"**{부서} — {보직}**", inline=False)
    if old_user_id:
        log_embed.add_field(name="해임 대상자", value=f"<@{old_user_id}>", inline=True)
    log_embed.add_field(name="명령권자", value=f"<@{operator.id}>", inline=False)
    log_embed.set_footer(text="ROKA-A Personnel Department")

    await log_action(interaction.guild, log_embed)
    await update_all_embeds(interaction.guild)
    await interaction.followup.send(f"✅ {부서}의 **{보직}** 자리를 해임 처리하여 공석으로 변경했습니다.")

@bot.tree.command(name="현황판설정", description="[관리자] 특정 부서 현황판의 타이틀, 설명, 색상, 이미지 등을 설정합니다.")
@app_commands.describe(부서="설정할 부서 이름", 타이틀="임베드 상단 제목", 설명="임베드 본문 설명", 색상="헥사코드 예: 0xff0000", 썸네일="우측 상단 이미지 URL", 이미지="하단 대형 이미지 URL", 푸터="하단 문구")
async def cmd_config_embed(interaction: discord.Interaction, 부서: str, 타이틀: str = None, 설명: str = None, 색상: str = None, 썸네일: str = None, 이미지: str = None, 푸터: str = None):
    if not is_head_admin(interaction.user):
        await interaction.response.send_message("❌ 이 명령어를 사용할 권한이 없습니다.", ephemeral=True)
        return

    if 부서 not in POSITIONS_BY_UNIT:
        await interaction.response.send_message(f"❌ '{부서}'은(는) 존재하지 않는 부서입니다. 구조 설정을 먼저 확인하세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if 부서 not in embed_config:
        embed_config[부서] = {}

    if 타이틀: embed_config[부서]["title"] = 타이틀
    if 설명: embed_config[부서]["description"] = 설명
    if 색상: embed_config[부서]["color"] = 색상
    if 썸네일: embed_config[부서]["thumbnail"] = 썸네일
    if 이미지: embed_config[부서]["image"] = 이미지
    if 푸터: embed_config[부서]["footer"] = 푸터

    save_json(EMBED_CONFIG_PATH, embed_config)
    await update_all_embeds(interaction.guild)
    await interaction.followup.send(f"✅ '{부서}' 현황판의 디자인 설정이 업데이트되었습니다.")

@bot.tree.command(name="구조설정", description="[관리자] 텍스트 형식으로 부서와 보직 구조를 대량으로 등록하거나 수정합니다.")
@app_commands.describe(구조텍스트="형식: [부서명] 보직1, 보직2 (줄바꿈으로 구분)")
async def cmd_set_structure(interaction: discord.Interaction, 구조텍스트: str):
    if not is_head_admin(interaction.user):
        await interaction.response.send_message("❌ 이 명령어를 사용할 권한이 없습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    global POSITIONS_BY_UNIT
    new_structure = {}
    
    lines = 구조텍스트.split("\n")
    current_unit = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        match = re.match(r"^\[(.+?)\]$", line)
        if match:
            current_unit = match.group(1).strip()
            new_structure[current_unit] = []
        else:
            if current_unit:
                items = [i.strip() for i in line.split(",") if i.strip()]
                new_structure[current_unit].extend(items)

    if not new_structure:
        await interaction.followup.send("❌ 올바른 구조 형식을 찾지 못했습니다. 예시: `[국방부]\n장관, 차관`")
        return

    POSITIONS_BY_UNIT = new_structure
    
    config_path = os.path.join(BASE_DIR, "structure_config.json")
    save_json(config_path, POSITIONS_BY_UNIT)

    await update_all_embeds(interaction.guild)
    await interaction.followup.send("✅ 부서 및 보직 직제 구조가 성공적으로 재설정되었으며 현황판에 반영되었습니다.")

# -----------------------------------------------------------------------------
# 이벤트 핸들러 (Events)
# -----------------------------------------------------------------------------

async def _sync_guilds_only():
    print("📋 전역 명령어가 아닌, 각 서버별 슬래시 명령어를 동기화합니다.")
    total = 0
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            total += len(synced)
            print(f"   [{guild.name}] 슬래시 명령어 {len(synced)}개 동기화 완료")
        except Exception as e:
            print(f"   [{guild.name}] 동기화 중 오류 발생: {e}")
    return total

async def _keep_alive_loop():
    """
    Render 환경으로 전환되면서 이 셀프 핑 로직은 활성화되지 않습니다.
    Render에서는 하단의 Flask 웹 서버와 UptimeRobot 연동을 통해 24시간 구동을 유지합니다.
    """
    domain = os.getenv("REPLIT_DEV_DOMAIN", "")
    if not domain:
        print("⚠️ REPLIT_DEV_DOMAIN 환경변수 없음 - 셀프 핑 비활성화 (Render 환경 자동 감지)")
        return

    ping_url = f"https://{domain}/api/healthz"
    print(f"🔮 Keep-alive 셀프 핑 시작: {ping_url} (4분 간격)")

    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"🔮 Keep-alive ping -> HTTP {resp.status}")
            except Exception as e:
                print(f"⚠️ Keep-alive ping 실패: {e}")
            await asyncio.sleep(240)

@bot.event
async def on_ready():
    global BOT_OWNER_ID, POSITIONS_BY_UNIT
    print(f"========================================")
    print(f"🤖 국방부 직제 관리 봇 가동 시작!")
    print(f"🤖 로그인 계정: {bot.user.name} ({bot.user.id})")
    print(f"========================================")

    if bot.owner_id:
        BOT_OWNER_ID = bot.owner_id
    else:
        try:
            app_info = await bot.application_info()
            BOT_OWNER_ID = app_info.owner.id
        except Exception as e:
            print(f"[WARN] 봇 소유자 정보를 가져오지 못했습니다: {e}")

    struct_path = os.path.join(BASE_DIR, "structure_config.json")
    POSITIONS_BY_UNIT = load_json(struct_path, {
        "군 수뇌부": ["육군참모총장", "육군참모차장"],
        "국방부": ["대통령", "국무총", "국방부장관", "국방부차관"]
    })

    if not bot.synced:
        await _sync_guilds_only()
        bot.synced = True

    bot.loop.create_task(_keep_alive_loop())

# =============================================================================
# 🚀 Render 배포를 위한 백그라운드 Flask 웹 서버 가동 로직
# =============================================================================
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "MND Bot is Alive and running on Render!"

def run_flask():
    # Render가 할당하는 환경변수 PORT를 자동 감지 (기본값 8080)
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# 봇 구동(run) 직전에 Flask를 쓰레드로 분리하여 백그라운드 실행
t = Thread(target=run_flask)
t.start()

# -----------------------------------------------------------------------------
# 봇 최종 실행
# -----------------------------------------------------------------------------
if __name__ or "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("[CRITICAL] 올바른 DISCORD_BOT 토큰이 환경 변수에 설정되지 않았습니다.")
    else:
        bot.run(TOKEN)
