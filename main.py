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

# ──────────────────────────────────────────────
# 계급·권한 설정
# ──────────────────────────────────────────────

STAR_RANKS: dict[str, int] = {
    "대장": 4,
    "중장": 3,
    "소장": 2,
    "준장": 1,
}

HEAD_ADMIN_KEYWORDS = ["국방부", "HeadAdmin"]
GENERAL_OFFICER_ROLE = "장성급 장교"
RESTRICTED_UNITS = ["군 수뇌부", "국방부"]

UNIT_NAMES = [
    "국방부",
    "군 수뇌부",
    "육군본부",
    "정보사령부",
    "지상작전사령부",
    "특수전사령부",
    "수도방위사령부",
    "교육사령부",
]

BOT_OWNER_ID: int = 0

# ──────────────────────────────────────────────
# POSITIONS_BY_UNIT
# dict: unit_name → list of (display_label, value)
#   value 포맷: "section|pos_name"  (section=""이면 본부 직책)
#   예: ("제9공수특전여단 참모장", "제9공수특전여단|참모장")
#       ("사령관",                "|사령관")
# ──────────────────────────────────────────────

POSITIONS_BY_UNIT: dict[str, list[tuple[str, str]]] = {}


# ──────────────────────────────────────────────
# JSON 유틸
# ──────────────────────────────────────────────


def load_json(path, default_val):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default_val


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# 직책 파싱 — 섹션(예하부대) 인식 포함
# ──────────────────────────────────────────────


def _clean_line(line: str) -> str:
    """Discord 이모지 태그와 볼드(**)를 제거한 뒤 공백 정리."""
    s = re.sub(r"<:[^:]+:\d+>", "", line)
    s = re.sub(r"\*+", "", s)
    return s.strip()


def _is_position_line(clean: str) -> re.Match | None:
    """'직책명: 공석' 또는 '직책명: <@...' 패턴인지 확인."""
    return re.match(r"^(.+?)\s*:\s*(공석|<@)", clean)


def parse_positions_with_context(description: str) -> list[tuple[str, str]]:
    """
    임베드 description 을 파싱해 (display, value) 리스트를 반환한다.

    - value 포맷: "section|pos_name"
      · section 이 비면 본부 직책 (예: "|사령관")
      · section 이 있으면 예하부대 직책 (예: "제9공수특전여단|참모장")
    - 중복 value 는 첫 등장만 유지.
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    current_section: str = ""

    for line in description.splitlines():
        clean = _clean_line(line)
        if not clean:
            continue

        m = _is_position_line(clean)
        if m:
            pos = m.group(1).strip()
            if not pos:
                continue

            value = f"{current_section}|{pos}"
            display = f"{current_section} {pos}" if current_section else pos

            if value not in seen:
                results.append((display, value))
                seen.add(value)

        else:
            # 섹션 헤더 후보: '[' 로 시작하는 줄은 건너뜀
            if clean.startswith("[") or clean.startswith("-"):
                continue

            # 선행 번호 "1. ", "2. " 등 제거
            sub_name = re.sub(r"^\d+\.\s*", "", clean).strip()
            if sub_name and len(sub_name) > 1:
                current_section = sub_name

    return results


def build_positions_by_unit() -> dict[str, list[tuple[str, str]]]:
    """embed_config.json 을 읽어 소속별 (display, value) 목록을 빌드한다."""
    if not os.path.exists(EMBED_CONFIG_PATH):
        return {}
    config = load_json(EMBED_CONFIG_PATH, {})
    raw_embeds = get_raw_embeds(config)
    result: dict[str, list[tuple[str, str]]] = {}
    for embed_data in raw_embeds:
        unit_name = ""
        author = embed_data.get("author")
        if isinstance(author, dict):
            unit_name = author.get("name", "")
        elif isinstance(author, str):
            unit_name = author
        if not unit_name:
            unit_name = embed_data.get("title", "")
        desc = embed_data.get("description", "")
        if unit_name and desc:
            result[unit_name] = parse_positions_with_context(desc)
    return result


# ──────────────────────────────────────────────
# 섹션 인식 임베드 라인 검색
# ──────────────────────────────────────────────


def find_position_line(
    lines: list[str],
    pos_value: str,
    want_vacant: bool,
) -> int:
    """
    pos_value ("section|pos_name") 에 해당하는 라인 인덱스를 반환한다.
    want_vacant=True  → '공석' 상태인 줄 검색 (임명 시)
    want_vacant=False → '<@'   상태인 줄 검색 (해임 시)
    못 찾으면 -1 반환.
    """
    if "|" in pos_value:
        section, pos_name = pos_value.split("|", 1)
    else:
        section, pos_name = "", pos_value

    marker = "공석" if want_vacant else "<@"

    if not section:
        # 본부 직책: 첫 번째 매치
        for i, line in enumerate(lines):
            if pos_name in line and marker in line:
                return i
        return -1

    # 예하부대 직책: 섹션 헤더를 먼저 찾고, 그 아래에서 검색
    in_section = False
    for i, line in enumerate(lines):
        clean = _clean_line(line)

        if not in_section:
            # 헤더 탐색: 섹션 이름이 들어있고, 직책 패턴이 아닌 줄
            if section in clean and not _is_position_line(clean):
                in_section = True
        else:
            # 해당 직책 줄인지 확인
            if pos_name in line and marker in line:
                return i
            # 다른 섹션으로 넘어갔는지 확인
            if clean and not clean.startswith("[") and not clean.startswith("-"):
                if not _is_position_line(clean):
                    sub = re.sub(r"^\d+\.\s*", "", clean).strip()
                    if sub and sub != section and len(sub) > 1:
                        in_section = False  # 새 섹션 진입 → 탐색 종료

    return -1


def display_pos(value: str) -> str:
    """value("section|pos") → 사람이 읽을 수 있는 문자열."""
    if "|" not in value:
        return value
    section, pos = value.split("|", 1)
    return f"{section} {pos}" if section else pos


# ──────────────────────────────────────────────
# 권한 헬퍼
# ──────────────────────────────────────────────


def is_owner(member: discord.Member) -> bool:
    if member.id == BOT_OWNER_ID:
        return True
    return any("부소유자" in role.name for role in member.roles)


def is_head_admin(member: discord.Member) -> bool:
    if is_owner(member):
        return True
    return any(
        any(kw in role.name for kw in HEAD_ADMIN_KEYWORDS) for role in member.roles
    )


def is_general_officer(member: discord.Member) -> bool:
    return is_head_admin(member) or any(
        role.name == GENERAL_OFFICER_ROLE for role in member.roles
    )


def get_member_unit(member: discord.Member) -> str | None:
    for unit in UNIT_NAMES:
        for role in member.roles:
            if unit in role.name or role.name in unit:
                return unit
    return None


def check_unit_permission(
    member: discord.Member, selected_unit: str
) -> tuple[bool, str]:
    if is_head_admin(member):
        return True, ""
    if selected_unit in RESTRICTED_UNITS:
        return False, (
            f"🚫 **접근 거부**\n"
            f"`{selected_unit}` 임베드는 `국방부` / `HeadAdmin` 역할 이상만 수정할 수 있습니다."
        )
    member_unit = get_member_unit(member)
    if member_unit is None:
        return False, (
            "🚫 **소속 없음**\n조직도에 등록된 소속 부대 역할이 없습니다.\n"
            "담당자에게 소속 역할 부여를 요청해 주세요."
        )
    if member_unit != selected_unit:
        return False, (
            f"🚫 **소속 불일치**\n귀하의 소속은 **{member_unit}** 입니다.\n"
            f"`{selected_unit}` 임베드는 수정할 수 없습니다."
        )
    return True, ""


def get_user_star_rank(member: discord.Member) -> int:
    max_rank = 0
    for role in member.roles:
        for keyword, rank in STAR_RANKS.items():
            if keyword in role.name:
                max_rank = max(max_rank, rank)
    return max_rank


def get_position_star_rank(pos_value: str) -> int:
    """pos_value("section|pos_name") 에서 별 계급을 추출."""
    pos_name = pos_value.split("|", 1)[1] if "|" in pos_value else pos_value
    for keyword, rank in STAR_RANKS.items():
        if keyword in pos_name:
            return rank
    return 0


def get_commander_position_value_in_unit(
    commander: discord.Member, unit: str
) -> str | None:
    """appointments.json 에서 commander 가 해당 unit 에서 맡은 직책의 value 를 반환."""
    appointments = load_json(APPOINTMENTS_PATH, {})
    unit_appts = appointments.get(unit, {})
    mention = commander.mention
    for pos_value, assigned in unit_appts.items():
        if assigned == mention:
            return pos_value
    return None


def check_unit_position_rank(
    commander: discord.Member, unit: str, target_value: str
) -> tuple[bool, str]:
    """
    같은 소속 내 직책 등급 비교.
    임베드 순서(위=높음)를 기준으로 commander 직책 인덱스 < target 인덱스여야 허용.
    """
    if is_head_admin(commander):
        return True, ""

    positions = POSITIONS_BY_UNIT.get(unit, [])  # [(display, value), ...]
    if not positions:
        return True, ""

    pos_values = [v for _, v in positions]

    commander_value = get_commander_position_value_in_unit(commander, unit)
    if commander_value is None:
        return True, ""

    try:
        commander_idx = pos_values.index(commander_value)
    except ValueError:
        return True, ""

    try:
        target_idx = pos_values.index(target_value)
    except ValueError:
        return True, ""

    if commander_idx >= target_idx:
        return False, (
            f"❌ **직책 등급 부족**\n"
            f"귀하의 직책(`{display_pos(commander_value)}`) 보다 높거나 같은 등급의 직책은 처리할 수 없습니다.\n"
            f"`{display_pos(target_value)}`은(는) 귀하의 직책과 같거나 더 높은 등급입니다.\n"
            f"상위 권한자에게 요청해 주세요."
        )
    return True, ""


def check_permission(
    commander: discord.Member,
    target: discord.Member | None,
    pos_value: str,
) -> tuple[bool, str]:
    if is_head_admin(commander):
        return True, ""

    if not is_general_officer(commander):
        return False, (
            "❌ **권한 없음**\n`장성급 장교` 역할이 있어야 인사 명령을 내릴 수 있습니다."
        )

    if target is not None and commander.id == target.id:
        return False, "❌ **자기 자신 처리 불가**\n본인을 직접 임명·해임할 수 없습니다."

    position_rank = get_position_star_rank(pos_value)
    if position_rank > 0:
        commander_rank = get_user_star_rank(commander)
        if commander_rank <= position_rank:
            pos_label = next(
                (k for k, v in STAR_RANKS.items() if v == position_rank), str(position_rank)
            )
            cmd_label = next(
                (k for k, v in STAR_RANKS.items() if v == commander_rank),
                f"{commander_rank}★",
            )
            return False, (
                f"❌ **계급 부족**\n"
                f"`{display_pos(pos_value)}` 직책은 **{pos_label}({position_rank}★)** 급 직위입니다.\n"
                f"현재 귀하의 계급({cmd_label}, {commander_rank}★)으로는 처리할 수 없습니다.\n"
                f"해당 직책을 처리하려면 **{position_rank + 1}★ 이상** 계급이 필요합니다."
            )
    return True, ""


# ──────────────────────────────────────────────
# 임베드 변환 / 복사 헬퍼
# ──────────────────────────────────────────────


def parse_color(val):
    if val is None:
        return 0x2F3136
    if isinstance(val, int):
        return val
    try:
        return int(str(val), 16)
    except Exception:
        return 0x2F3136


def build_discord_embed(embed_data: dict) -> discord.Embed:
    embed = discord.Embed(
        title=embed_data.get("title") or None,
        description=embed_data.get("description") or None,
        color=parse_color(embed_data.get("color")),
        url=embed_data.get("url") or None,
    )
    author = embed_data.get("author")
    if author:
        if isinstance(author, dict):
            embed.set_author(
                name=author.get("name", ""),
                icon_url=author.get("icon_url") or None,
                url=author.get("url") or None,
            )
        else:
            embed.set_author(name=str(author))

    thumbnail = embed_data.get("thumbnail")
    if thumbnail:
        url = thumbnail.get("url") if isinstance(thumbnail, dict) else thumbnail
        if url:
            embed.set_thumbnail(url=url)

    image = embed_data.get("image")
    if image:
        url = image.get("url") if isinstance(image, dict) else image
        if url:
            embed.set_image(url=url)

    footer = embed_data.get("footer")
    if footer:
        if isinstance(footer, dict):
            embed.set_footer(
                text=footer.get("text", ""),
                icon_url=footer.get("icon_url") or None,
            )
        else:
            embed.set_footer(text=str(footer))

    for field in embed_data.get("fields", []):
        embed.add_field(
            name=field.get("name", ""),
            value=field.get("value", ""),
            inline=field.get("inline", False),
        )
    return embed


def live_embed_copy(live: discord.Embed, new_description: str) -> discord.Embed:
    new = discord.Embed(
        title=live.title,
        description=new_description,
        color=live.color,
        url=live.url,
    )
    if live.author:
        new.set_author(
            name=live.author.name,
            icon_url=live.author.icon_url or None,
            url=live.author.url or None,
        )
    if live.thumbnail:
        new.set_thumbnail(url=live.thumbnail.url)
    if live.image:
        new.set_image(url=live.image.url)
    for f in live.fields:
        new.add_field(name=f.name, value=f.value, inline=f.inline)
    if live.footer:
        new.set_footer(text=live.footer.text, icon_url=live.footer.icon_url or None)
    return new


def get_raw_embeds(config: dict) -> list:
    if "messages" in config:
        result = []
        for msg in config["messages"]:
            data = msg.get("data", msg)
            result.extend(data.get("embeds", []))
        return result
    return config.get("embeds", [])


# ──────────────────────────────────────────────
# 히스토리 (임명취소용)
# ──────────────────────────────────────────────


def save_history(unit_name: str, pos_value: str, previous_value: str):
    history = load_json(HISTORY_PATH, {})
    if unit_name not in history:
        history[unit_name] = {}
    history[unit_name][pos_value] = previous_value
    save_json(HISTORY_PATH, history)


def pop_history(unit_name: str, pos_value: str) -> str:
    history = load_json(HISTORY_PATH, {})
    return history.get(unit_name, {}).get(pos_value, "공석")


# ──────────────────────────────────────────────
# 로그 채널 전송
# ──────────────────────────────────────────────


async def send_action_log(
    action_type: str,
    commander: discord.Member,
    unit_name: str,
    pos_value: str,
    target_user: discord.Member | None = None,
):
    try:
        log_channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
    except Exception as e:
        print(f"[오류] 로그 채널 ID({LOG_CHANNEL_ID}) 접근 실패: {e}")
        return
    if not log_channel:
        return

    now_time = datetime.now().strftime("%Y년 %m월 %d일 %H시 %M분 %S초")
    embed = discord.Embed(title="⬛  인사 명령 발령 로그", color=0x36393F)
    embed.add_field(name="⬛ 발령 일시", value=f"`{now_time}`", inline=False)
    embed.add_field(name="⬛ 명령 종류", value=f"**{action_type}**", inline=True)
    embed.add_field(
        name="⬛ 명령권자",
        value=f"{commander.mention} (`{commander.display_name}`)",
        inline=True,
    )
    embed.add_field(name="⬛ 발령 소속", value=f"**{unit_name}**", inline=False)
    embed.add_field(name="⬛ 직책", value=f"**{display_pos(pos_value)}**", inline=True)
    target_val = (
        f"{target_user.mention} (`{target_user.display_name}`)"
        if target_user
        else "공석 처리"
    )
    embed.add_field(name="⬛ 대상자", value=target_val, inline=True)
    embed.set_footer(text="RTC 국방인사정보체계 | 본 로그는 자동 생성됩니다.")
    embed.timestamp = datetime.now()
    await log_channel.send(embed=embed)


# ──────────────────────────────────────────────
# 자동완성 헬퍼
# ──────────────────────────────────────────────


def _position_choices(unit: str | None, current: str) -> list[app_commands.Choice]:
    if not unit:
        return []
    positions = POSITIONS_BY_UNIT.get(unit, [])
    filtered = [(d, v) for d, v in positions if current in d]
    return [app_commands.Choice(name=d, value=v) for d, v in filtered[:25]]


# ──────────────────────────────────────────────
# 봇 초기화
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


async def _sync_guilds_only():
    saved_commands = bot.tree.get_commands()
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    print("   🗑️ 전역(global) 명령어 제거 완료 (중복 방지)")
    for cmd in saved_commands:
        bot.tree.add_command(cmd)
    total = 0
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        total += len(synced)
        print(f"   [{guild.name}] 슬래시 명령어 {len(synced)}개 동기화")
    return total


async def _keep_alive_loop():
    """
    4분마다 /api/healthz 를 셀프 핑해서 Replit 프로젝트가 잠들지 않도록 유지한다.
    UptimeRobot(5분) + 봇 자체 핑(4분) 이중 구조.
    """
    domain = os.getenv("REPLIT_DEV_DOMAIN", "")
    if not domain:
        print("   ⚠️ REPLIT_DEV_DOMAIN 환경변수 없음 — 셀프 핑 비활성화")
        return

    ping_url = f"https://{domain}/api/healthz"
    print(f"   🏓 Keep-alive 셀프 핑 시작: {ping_url} (4분 간격)")

    await asyncio.sleep(60)  # 봇 완전 기동 후 1분 대기
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    ping_url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    print(f"   🏓 Keep-alive ping → HTTP {resp.status}")
            except Exception as e:
                print(f"   ⚠️ Keep-alive ping 실패: {e}")
            await asyncio.sleep(240)  # 4분 대기


@bot.event
async def on_ready():
    global BOT_OWNER_ID, POSITIONS_BY_UNIT
    app_info = await bot.application_info()
    BOT_OWNER_ID = app_info.owner.id
    POSITIONS_BY_UNIT = build_positions_by_unit()
    print(f"🎖️ [RTC 국방인사정보체계] 초기화 완료. 가동 봇: {bot.user}")
    print(f"   봇 소유자: {app_info.owner} (ID: {BOT_OWNER_ID})")
    print(f"   조직도 채널 ID: {ORG_CHANNEL_ID}")
    print(f"   로그 채널 ID:   {LOG_CHANNEL_ID}")
    for unit, positions in POSITIONS_BY_UNIT.items():
        print(f"   [{unit}] 직책 {len(positions)}개 파싱")
    total = await _sync_guilds_only()
    print(f"   ✅ 전체 동기화 완료: {total}개 (중복 없음)")
    bot.loop.create_task(_keep_alive_loop())


# ──────────────────────────────────────────────
# /조직도생성
# ──────────────────────────────────────────────


async def _delete_tracked_messages(org_channel):
    message_ids = load_json(MESSAGE_IDS_PATH, [])
    deleted = 0
    for msg_id in message_ids:
        if not msg_id:
            continue
        try:
            msg = await org_channel.fetch_message(msg_id)
            await msg.delete()
            deleted += 1
        except Exception:
            pass
    return deleted


@bot.tree.command(name="조직도생성", description="조직도 임베드를 채널에 게시합니다. (국방부/HeadAdmin 전용)")
@app_commands.describe(임베드번호="특정 임베드만 재게시 (1~8). 생략 시 전체 재게시.")
async def create_org(interaction: discord.Interaction, 임베드번호: int = None):
    global POSITIONS_BY_UNIT
    await interaction.response.defer(ephemeral=True)

    if not is_head_admin(interaction.user):
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ 권한 거부",
                description="`국방부` / `HeadAdmin` / `부소유자` 역할만 조직도를 생성할 수 있습니다.",
                color=0xFF4444,
            ),
            ephemeral=True,
        )
        return

    try:
        org_channel = bot.get_channel(ORG_CHANNEL_ID) or await bot.fetch_channel(ORG_CHANNEL_ID)
    except Exception:
        await interaction.followup.send("❌ 조직도 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    if not os.path.exists(EMBED_CONFIG_PATH):
        await interaction.followup.send("❌ `embed_config.json` 파일이 없습니다.", ephemeral=True)
        return

    config = load_json(EMBED_CONFIG_PATH, {})
    raw_embeds = get_raw_embeds(config)

    if not raw_embeds:
        await interaction.followup.send("❌ `embed_config.json`에서 임베드를 찾을 수 없습니다.", ephemeral=True)
        return

    # ── 특정 임베드만 재게시 ──
    if 임베드번호 is not None:
        idx = 임베드번호 - 1
        if idx < 0 or idx >= len(raw_embeds):
            await interaction.followup.send(
                f"❌ 임베드 번호 `{임베드번호}`이 범위를 벗어났습니다. (사용 가능: 1 ~ {len(raw_embeds)})",
                ephemeral=True,
            )
            return

        message_ids = load_json(MESSAGE_IDS_PATH, [])
        if idx < len(message_ids) and message_ids[idx]:
            try:
                old_msg = await org_channel.fetch_message(message_ids[idx])
                await old_msg.delete()
            except Exception:
                pass

        while len(message_ids) <= idx:
            message_ids.append(None)

        discord_embed = build_discord_embed(raw_embeds[idx])
        msg = await org_channel.send(embed=discord_embed)
        message_ids[idx] = msg.id
        save_json(MESSAGE_IDS_PATH, message_ids)
        POSITIONS_BY_UNIT = build_positions_by_unit()

        await interaction.followup.send(
            f"✅ **임베드 {임베드번호}번** 재게시 완료 (메시지 ID: `{msg.id}`).\n"
            f"조직도 채널: {org_channel.mention}",
            ephemeral=True,
        )
        return

    # ── 전체 재게시 (기존 메시지 삭제 후) ──
    deleted = await _delete_tracked_messages(org_channel)

    content_text = config.get("content") or None
    message_ids = []
    for idx, embed_data in enumerate(raw_embeds):
        discord_embed = build_discord_embed(embed_data)
        if idx == 0 and content_text:
            msg = await org_channel.send(content=content_text, embed=discord_embed)
        else:
            msg = await org_channel.send(embed=discord_embed)
        message_ids.append(msg.id)

    save_json(MESSAGE_IDS_PATH, message_ids)
    save_json(APPOINTMENTS_PATH, {})
    POSITIONS_BY_UNIT = build_positions_by_unit()

    await interaction.followup.send(
        f"✅ 조직도 생성 완료\n"
        f"• 기존 메시지 {deleted}개 삭제\n"
        f"• 새 임베드 **{len(message_ids)}개** 게시\n"
        f"조직도 채널: {org_channel.mention}",
        ephemeral=True,
    )


# ──────────────────────────────────────────────
# /임명
# ──────────────────────────────────────────────


@bot.tree.command(name="임명", description="조직도 공석 직책에 대상자를 임명합니다.")
@app_commands.describe(
    소속="임명할 부대/기관을 선택하세요",
    대상자="임명할 유저를 선택하세요",
    직책명="소속 선택 후 자동완성으로 고르세요",
    사유="임명 사유 (선택 사항)",
)
@app_commands.choices(소속=[app_commands.Choice(name=u, value=u) for u in UNIT_NAMES])
async def slash_appoint(
    interaction: discord.Interaction,
    소속: str,
    대상자: discord.Member,
    직책명: str,
    사유: str = None,
):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user

    unit_ok, unit_reason = check_unit_permission(member, 소속)
    if not unit_ok:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 소속 제한", description=unit_reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    allowed, reason = check_permission(member, 대상자, 직책명)
    if not allowed:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 인사 명령 거부", description=reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    pos_ok, pos_reason = check_unit_position_rank(member, 소속, 직책명)
    if not pos_ok:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 직책 등급 제한", description=pos_reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    try:
        org_channel = bot.get_channel(ORG_CHANNEL_ID) or await bot.fetch_channel(ORG_CHANNEL_ID)
    except Exception:
        await interaction.followup.send("❌ 조직도 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    message_ids = load_json(MESSAGE_IDS_PATH, [])
    if not message_ids:
        await interaction.followup.send(
            "❌ 등록된 조직도 메시지가 없습니다. `/조직도생성`을 먼저 실행해 주세요.", ephemeral=True
        )
        return

    # value 포맷 정규화: 수동 입력은 "|직책명" 으로 처리
    pos_value = 직책명 if "|" in 직책명 else f"|{직책명}"
    pos_name_only = pos_value.split("|", 1)[1]

    is_modified = False
    not_found_count = 0
    for msg_id in message_ids:
        if not msg_id:
            continue
        try:
            msg = await org_channel.fetch_message(msg_id)
            live = msg.embeds[0] if msg.embeds else None
            if not live or not live.description:
                continue

            embed_unit = (live.author.name if live.author else None) or live.title or ""
            if 소속 not in embed_unit and embed_unit not in 소속:
                continue

            lines = live.description.splitlines()
            target_idx = find_position_line(lines, pos_value, want_vacant=True)
            if target_idx == -1:
                continue

            lines[target_idx] = lines[target_idx].replace("공석", 대상자.mention, 1)
            await msg.edit(embed=live_embed_copy(live, "\n".join(lines)))
            is_modified = True
            break

        except discord.NotFound:
            not_found_count += 1
        except Exception as e:
            print(f"[오류] /임명 처리: {e}")

    if is_modified:
        appointments = load_json(APPOINTMENTS_PATH, {})
        prev = appointments.get(소속, {}).get(pos_value, "공석")
        save_history(소속, pos_value, prev)
        appointments.setdefault(소속, {})[pos_value] = 대상자.mention
        save_json(APPOINTMENTS_PATH, appointments)

        result_embed = discord.Embed(
            title="✅ 인사 명령 발령",
            description=f"`{소속}` — `{display_pos(pos_value)}` 직책에 {대상자.mention} 임명 완료.",
            color=0x00CC44,
        )
        if 사유:
            result_embed.add_field(name="📋 사유", value=사유, inline=False)
        await interaction.followup.send(embed=result_embed, ephemeral=True)
        await send_action_log("임명", member, 소속, pos_value, 대상자)

    elif not_found_count > 0:
        await interaction.followup.send(
            f"❌ 조직도 메시지를 찾을 수 없습니다 ({not_found_count}개 만료).\n"
            "`/조직도생성`을 실행해서 조직도를 재게시해 주세요.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ 명령 미발령",
                description=(
                    f"조직도에서 아래 조건을 동시에 만족하는 줄을 찾지 못했습니다.\n\n"
                    f"• 소속: `{소속}`\n• 직책명: `{display_pos(pos_value)}`\n• 상태: `공석`\n\n"
                    "**확인 사항:**\n"
                    "• 자동완성 목록에서 직책을 선택해 주세요.\n"
                    "• 이미 임명된 직책은 `/해임`을 먼저 실행하세요."
                ),
                color=0xFF4444,
            ),
            ephemeral=True,
        )


@slash_appoint.autocomplete("직책명")
async def appoint_position_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    unit = getattr(interaction.namespace, "소속", None)
    return _position_choices(unit, current)


# ──────────────────────────────────────────────
# /해임
# ──────────────────────────────────────────────


@bot.tree.command(name="해임", description="조직도 직책의 현 임명자를 해임(공석 처리)합니다.")
@app_commands.describe(
    소속="해임할 부대/기관을 선택하세요",
    직책명="소속 선택 후 자동완성으로 고르세요",
    사유="해임 사유 (선택 사항)",
)
@app_commands.choices(소속=[app_commands.Choice(name=u, value=u) for u in UNIT_NAMES])
async def slash_dismiss(
    interaction: discord.Interaction,
    소속: str,
    직책명: str,
    사유: str = None,
):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user

    unit_ok, unit_reason = check_unit_permission(member, 소속)
    if not unit_ok:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 소속 제한", description=unit_reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    allowed, reason = check_permission(member, None, 직책명)
    if not allowed:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 인사 명령 거부", description=reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    pos_ok, pos_reason = check_unit_position_rank(member, 소속, 직책명)
    if not pos_ok:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 직책 등급 제한", description=pos_reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    try:
        org_channel = bot.get_channel(ORG_CHANNEL_ID) or await bot.fetch_channel(ORG_CHANNEL_ID)
    except Exception:
        await interaction.followup.send("❌ 조직도 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    message_ids = load_json(MESSAGE_IDS_PATH, [])
    if not message_ids:
        await interaction.followup.send(
            "❌ 등록된 조직도 메시지가 없습니다. `/조직도생성`을 먼저 실행해 주세요.", ephemeral=True
        )
        return

    pos_value = 직책명 if "|" in 직책명 else f"|{직책명}"

    is_modified = False
    not_found_count = 0
    for msg_id in message_ids:
        if not msg_id:
            continue
        try:
            msg = await org_channel.fetch_message(msg_id)
            live = msg.embeds[0] if msg.embeds else None
            if not live or not live.description:
                continue

            embed_unit = (live.author.name if live.author else None) or live.title or ""
            if 소속 not in embed_unit and embed_unit not in 소속:
                continue

            lines = live.description.splitlines()
            target_idx = find_position_line(lines, pos_value, want_vacant=False)
            if target_idx == -1:
                continue

            lines[target_idx] = re.sub(r"<@!?\d+>", "공석", lines[target_idx], count=1)
            await msg.edit(embed=live_embed_copy(live, "\n".join(lines)))
            is_modified = True
            break

        except discord.NotFound:
            not_found_count += 1
        except Exception as e:
            print(f"[오류] /해임 처리: {e}")

    if is_modified:
        appointments = load_json(APPOINTMENTS_PATH, {})
        prev = appointments.get(소속, {}).get(pos_value, "공석")
        save_history(소속, pos_value, prev)
        if 소속 in appointments and pos_value in appointments[소속]:
            del appointments[소속][pos_value]
            if not appointments[소속]:
                del appointments[소속]
        save_json(APPOINTMENTS_PATH, appointments)

        result_embed = discord.Embed(
            title="✅ 인사 명령 발령",
            description=f"`{소속}` — `{display_pos(pos_value)}` 직책이 해임(공석 처리)되었습니다.",
            color=0xFF4444,
        )
        if 사유:
            result_embed.add_field(name="📋 사유", value=사유, inline=False)
        await interaction.followup.send(embed=result_embed, ephemeral=True)
        await send_action_log("해임", member, 소속, pos_value, None)

    elif not_found_count > 0:
        await interaction.followup.send(
            f"❌ 조직도 메시지를 찾을 수 없습니다 ({not_found_count}개 만료).\n"
            "`/조직도생성`을 실행해서 조직도를 재게시해 주세요.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "❌ 이미 공석이거나 해당 직책 라인을 찾지 못했습니다.", ephemeral=True
        )


@slash_dismiss.autocomplete("직책명")
async def dismiss_position_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    unit = getattr(interaction.namespace, "소속", None)
    return _position_choices(unit, current)


# ──────────────────────────────────────────────
# /임명취소
# ──────────────────────────────────────────────


@bot.tree.command(
    name="임명취소",
    description="임명을 취소합니다. @대상자만: 전체 공석 처리 | 소속+직책명: 직전 상태로 복구",
)
@app_commands.describe(
    대상자="이 유저의 모든 직책을 공석으로 처리 (소속/직책명 없이 단독 사용)",
    소속="되돌릴 직책의 소속 부대 (직책명과 함께 사용)",
    직책명="되돌릴 직책명 (소속과 함께 사용)",
)
@app_commands.choices(소속=[app_commands.Choice(name=u, value=u) for u in UNIT_NAMES])
async def slash_undo(
    interaction: discord.Interaction,
    대상자: discord.Member = None,
    소속: str = None,
    직책명: str = None,
):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user

    # ── 모드 A: 유저 전체 공석 처리 ──
    if 대상자 is not None:
        if not is_general_officer(member):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🚫 인사 명령 거부",
                    description="`장성급 장교` 역할 이상만 임명취소를 내릴 수 있습니다.",
                    color=0xFF4444,
                ),
                ephemeral=True,
            )
            return

        target_pattern = re.compile(r"<@!?" + str(대상자.id) + r">")

        try:
            org_channel = bot.get_channel(ORG_CHANNEL_ID) or await bot.fetch_channel(ORG_CHANNEL_ID)
        except Exception:
            await interaction.followup.send("❌ 조직도 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        message_ids = load_json(MESSAGE_IDS_PATH, [])
        if not message_ids:
            await interaction.followup.send(
                "❌ 등록된 조직도 메시지가 없습니다. `/조직도생성`을 먼저 실행해 주세요.", ephemeral=True
            )
            return

        removed_positions: list[tuple[str, str]] = []
        for msg_id in message_ids:
            if not msg_id:
                continue
            try:
                msg = await org_channel.fetch_message(msg_id)
                live = msg.embeds[0] if msg.embeds else None
                if not live or not live.description:
                    continue
                if not target_pattern.search(live.description):
                    continue
                embed_unit = (live.author.name if live.author else None) or live.title or "알 수 없음"
                lines = live.description.splitlines()
                new_lines = []
                changed = False
                for line in lines:
                    if target_pattern.search(line):
                        col = line.find(":")
                        if col != -1:
                            label = _clean_line(line[:col]).split()
                            pos_guess = label[-1] if label else "직책"
                            removed_positions.append((embed_unit, f"|{pos_guess}"))
                        line = target_pattern.sub("공석", line)
                        changed = True
                    new_lines.append(line)
                if changed:
                    await msg.edit(embed=live_embed_copy(live, "\n".join(new_lines)))
            except discord.NotFound:
                continue
            except Exception as e:
                print(f"[오류] /임명취소(유저): {e}")

        if removed_positions:
            appointments = load_json(APPOINTMENTS_PATH, {})
            for u, pv in removed_positions:
                if u in appointments and pv in appointments[u]:
                    del appointments[u][pv]
                    if not appointments[u]:
                        del appointments[u]
            save_json(APPOINTMENTS_PATH, appointments)
            summary = "\n".join(f"• `{u}` — `{display_pos(pv)}`" for u, pv in removed_positions)
            await interaction.followup.send(
                f"↩️ **[임명취소]** {대상자.mention} 의 직책이 공석 처리되었습니다.\n{summary}",
                ephemeral=True,
            )
            await send_action_log(
                "임명취소(유저)",
                member,
                ", ".join(u for u, _ in removed_positions),
                ", ".join(pv for _, pv in removed_positions),
                None,
            )
        else:
            await interaction.followup.send(
                f"❌ 조직도에서 {대상자.mention} 를 찾을 수 없습니다.", ephemeral=True
            )
        return

    # ── 모드 B: 직책 하나 복구 ──
    if 소속 is None or 직책명 is None:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ 입력 오류",
                description=(
                    "**사용법 A — 유저 전체 해제:**\n`/임명취소 대상자:@유저`\n\n"
                    "**사용법 B — 직책 되돌리기:**\n`/임명취소 소속:부대명 직책명:직책명`"
                ),
                color=0xFF4444,
            ),
            ephemeral=True,
        )
        return

    pos_value = 직책명 if "|" in 직책명 else f"|{직책명}"

    allowed, reason = check_permission(member, None, pos_value)
    if not allowed:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 인사 명령 거부", description=reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    pos_ok, pos_reason = check_unit_position_rank(member, 소속, pos_value)
    if not pos_ok:
        await interaction.followup.send(
            embed=discord.Embed(title="🚫 직책 등급 제한", description=pos_reason, color=0xFF4444),
            ephemeral=True,
        )
        return

    try:
        org_channel = bot.get_channel(ORG_CHANNEL_ID) or await bot.fetch_channel(ORG_CHANNEL_ID)
    except Exception:
        await interaction.followup.send("❌ 조직도 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    previous_value = pop_history(소속, pos_value)
    message_ids = load_json(MESSAGE_IDS_PATH, [])
    if not message_ids:
        await interaction.followup.send("❌ 등록된 조직도 메시지가 없습니다.", ephemeral=True)
        return

    is_modified = False
    for msg_id in message_ids:
        if not msg_id:
            continue
        try:
            msg = await org_channel.fetch_message(msg_id)
            live = msg.embeds[0] if msg.embeds else None
            if not live or not live.description:
                continue
            embed_unit = (live.author.name if live.author else None) or live.title or ""
            if 소속 not in embed_unit and embed_unit not in 소속:
                continue
            lines = live.description.splitlines()
            # find any state (공석 or <@)
            target_idx = find_position_line(lines, pos_value, want_vacant=True)
            if target_idx == -1:
                target_idx = find_position_line(lines, pos_value, want_vacant=False)
            if target_idx == -1:
                continue

            # 공석으로 먼저 초기화한 뒤 이전 값으로 교체
            line = re.sub(r"<@!?\d+>", "공석", lines[target_idx])
            if previous_value != "공석":
                line = line.replace("공석", previous_value, 1)
            lines[target_idx] = line
            await msg.edit(embed=live_embed_copy(live, "\n".join(lines)))
            is_modified = True
            break
        except discord.NotFound:
            continue
        except Exception as e:
            print(f"[오류] /임명취소 처리: {e}")

    if is_modified:
        appointments = load_json(APPOINTMENTS_PATH, {})
        if previous_value == "공석":
            if 소속 in appointments and pos_value in appointments[소속]:
                del appointments[소속][pos_value]
                if not appointments[소속]:
                    del appointments[소속]
        else:
            appointments.setdefault(소속, {})[pos_value] = previous_value
        save_json(APPOINTMENTS_PATH, appointments)
        label = f"`{previous_value}`" if previous_value != "공석" else "**공석**"
        await interaction.followup.send(
            f"↩️ **[임명취소]** `{소속}` — `{display_pos(pos_value)}` 직책을 이전 상태({label})로 복구했습니다.",
            ephemeral=True,
        )
        await send_action_log("임명취소", member, 소속, pos_value, None)
    else:
        await interaction.followup.send(
            "❌ 해당 직책 라인을 찾지 못했습니다. `/조직도확인`으로 상태를 확인해 주세요.",
            ephemeral=True,
        )


@slash_undo.autocomplete("직책명")
async def undo_position_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    unit = getattr(interaction.namespace, "소속", None)
    return _position_choices(unit, current)


# ──────────────────────────────────────────────
# /조직도리셋
# ──────────────────────────────────────────────


@bot.tree.command(name="조직도리셋", description="기존 조직도를 삭제하고 처음부터 재생성합니다. (국방부/HeadAdmin 전용)")
async def reset_org(interaction: discord.Interaction):
    global POSITIONS_BY_UNIT
    await interaction.response.defer(ephemeral=True)

    if not is_head_admin(interaction.user):
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ 권한 거부",
                description="`국방부` / `HeadAdmin` / `부소유자` 역할만 조직도를 리셋할 수 있습니다.",
                color=0xFF4444,
            ),
            ephemeral=True,
        )
        return

    try:
        org_channel = bot.get_channel(ORG_CHANNEL_ID) or await bot.fetch_channel(ORG_CHANNEL_ID)
    except Exception:
        await interaction.followup.send("❌ 조직도 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    if not os.path.exists(EMBED_CONFIG_PATH):
        await interaction.followup.send("❌ `embed_config.json` 파일이 없습니다.", ephemeral=True)
        return

    deleted = await _delete_tracked_messages(org_channel)
    save_json(MESSAGE_IDS_PATH, [])
    save_json(APPOINTMENTS_PATH, {})
    save_json(HISTORY_PATH, {})

    config = load_json(EMBED_CONFIG_PATH, {})
    raw_embeds = get_raw_embeds(config)
    content_text = config.get("content") or None
    new_ids = []

    for idx, embed_data in enumerate(raw_embeds):
        discord_embed = build_discord_embed(embed_data)
        if idx == 0 and content_text:
            msg = await org_channel.send(content=content_text, embed=discord_embed)
        else:
            msg = await org_channel.send(embed=discord_embed)
        new_ids.append(msg.id)

    save_json(MESSAGE_IDS_PATH, new_ids)
    POSITIONS_BY_UNIT = build_positions_by_unit()

    await interaction.followup.send(
        f"🔄 **조직도 리셋 완료**\n"
        f"• 기존 메시지 {deleted}개 삭제\n"
        f"• 새 임베드 **{len(new_ids)}개** 재게시\n"
        f"• 임명 기록 + 히스토리 전부 초기화",
        ephemeral=True,
    )


# ──────────────────────────────────────────────
# /조직도확인
# ──────────────────────────────────────────────


@bot.tree.command(name="조직도확인", description="소속별 직책 임명 현황을 테이블로 확인합니다.")
@app_commands.describe(소속="확인할 부대를 선택하세요. 생략 시 전체 요약을 표시합니다.")
@app_commands.choices(소속=[app_commands.Choice(name=u, value=u) for u in UNIT_NAMES])
async def check_org(interaction: discord.Interaction, 소속: str = None):
    await interaction.response.defer(ephemeral=True)
    message_ids = load_json(MESSAGE_IDS_PATH, [])
    appointments = load_json(APPOINTMENTS_PATH, {})

    if not message_ids:
        await interaction.followup.send(
            "⚠️ 등록된 조직도 메시지가 없습니다. `/조직도생성`을 먼저 실행해 주세요.",
            ephemeral=True,
        )
        return

    # ── 특정 소속 상세 보기 ──
    if 소속 is not None:
        positions = POSITIONS_BY_UNIT.get(소속, [])
        unit_appts = appointments.get(소속, {})

        filled = sum(1 for _, pv in positions if pv in unit_appts)
        vacant = len(positions) - filled
        color = 0x00CC44 if vacant == 0 else (0xFFAA00 if filled > 0 else 0x2F3136)

        embed = discord.Embed(
            title=f"🎖️ {소속} — 인사 현황",
            description=f"**임명 완료 {filled}개** / 공석 {vacant}개 / 전체 {len(positions)}개",
            color=color,
        )

        if not positions:
            embed.add_field(name="⚠️ 정보 없음", value="직책 데이터가 없습니다. `/조직도생성` 후 다시 시도해 주세요.", inline=False)
        else:
            # 섹션별로 묶어서 출력
            current_section = ""
            section_lines: list[str] = []

            def flush_section(sec: str, lines: list[str]):
                if not lines:
                    return
                field_name = f"📂 {sec}" if sec else "🏛️ 본부"
                # Discord field value 1024자 제한 대응
                chunk = "\n".join(lines)
                if len(chunk) > 1020:
                    chunk = chunk[:1017] + "…"
                embed.add_field(name=field_name, value=chunk, inline=False)

            for display, pv in positions:
                # section 추출
                section = pv.split("|", 1)[0] if "|" in pv else ""
                pos_name = pv.split("|", 1)[1] if "|" in pv else pv

                if section != current_section:
                    flush_section(current_section, section_lines)
                    current_section = section
                    section_lines = []

                if pv in unit_appts:
                    mention = unit_appts[pv]
                    section_lines.append(f"🟢 **{pos_name}** — {mention}")
                else:
                    section_lines.append(f"⬜ **{pos_name}** — `공석`")

            flush_section(current_section, section_lines)

        embed.set_footer(text=f"RTC 국방인사정보체계 | {소속} 인사 현황")
        embed.timestamp = datetime.now()
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # ── 전체 요약 보기 ──
    total_positions = sum(len(v) for v in POSITIONS_BY_UNIT.values())
    total_filled = sum(
        len(unit_appts) for unit_appts in appointments.values()
    )
    total_vacant = total_positions - total_filled

    summary_embed = discord.Embed(
        title="📋 RTC 전군 인사 현황 요약",
        description=(
            f"🟢 **임명 완료** {total_filled}개　"
            f"⬜ **공석** {total_vacant}개　"
            f"📊 **전체** {total_positions}개"
        ),
        color=0x2F3136,
    )
    summary_embed.timestamp = datetime.now()

    for unit in UNIT_NAMES:
        positions = POSITIONS_BY_UNIT.get(unit, [])
        unit_appts = appointments.get(unit, {})
        filled = sum(1 for _, pv in positions if pv in unit_appts)
        total = len(positions)
        vacant = total - filled

        if total == 0:
            status = "⚪ 데이터 없음"
        elif vacant == 0:
            status = "🟢 전원 임명"
        elif filled == 0:
            status = "🔴 전원 공석"
        else:
            bar_filled = round(filled / total * 8)
            bar = "█" * bar_filled + "░" * (8 - bar_filled)
            status = f"`{bar}` {filled}/{total}"

        # 임명된 인원 간략 목록 (최대 3명)
        appointed_list = []
        for _, pv in positions:
            if pv in unit_appts:
                pos_name = pv.split("|", 1)[1] if "|" in pv else pv
                mention = unit_appts[pv]
                appointed_list.append(f"`{pos_name}` {mention}")
            if len(appointed_list) >= 3:
                remaining = filled - 3
                if remaining > 0:
                    appointed_list.append(f"…외 {remaining}명")
                break

        field_val = status
        if appointed_list:
            field_val += "\n" + "\n".join(appointed_list)

        summary_embed.add_field(name=f"🏛️ {unit}", value=field_val, inline=True)

    # 메시지 ID 정보
    valid_ids = [mid for mid in message_ids if mid]
    summary_embed.set_footer(
        text=f"조직도 메시지 {len(valid_ids)}개 등록됨 | /조직도확인 소속:[부대명] 으로 상세 조회"
    )

    await interaction.followup.send(embed=summary_embed, ephemeral=True)


# ──────────────────────────────────────────────
# /인사행정처리안내
# ──────────────────────────────────────────────


@bot.tree.command(name="인사행정처리안내", description="RTC 국방인사정보체계 명령어 사용 규칙 및 권한 전체 안내")
async def admin_guide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 인사행정처리 안내",
        description="RTC 국방인사정보체계 명령어 사용 규칙 및 권한 안내입니다.",
        color=0x2F3136,
    )
    embed.add_field(name="✅ /임명  소속  대상자  직책명  [사유]",
        value="소속 선택 후 직책명 자동완성 활성화. 공석 직책에 대상자를 임명합니다.", inline=False)
    embed.add_field(name="🗑️ /해임  소속  직책명  [사유]",
        value="소속 선택 후 직책명 자동완성 활성화. 해당 직책을 공석으로 처리합니다.", inline=False)
    embed.add_field(name="↩️ /임명취소  [대상자 또는 소속+직책명]",
        value="`대상자`만: 해당 유저의 모든 직책 공석 처리\n`소속+직책명`: 직전 명령을 한 단계 되돌립니다.", inline=False)
    embed.add_field(name="─────────────────────────────", value="**🛡️ 권한 및 제한 규칙**", inline=False)
    embed.add_field(name="1️⃣ 기본 자격",
        value="`장성급 장교` 역할 보유자만 인사 명령 사용 가능.\n`국방부`/`HeadAdmin`/`부소유자`는 모든 제한 초월.", inline=False)
    embed.add_field(name="1️⃣-1 소속 제한",
        value="본인 소속 부대만 처리 가능. `군 수뇌부`/`국방부`는 HeadAdmin 전용.", inline=False)
    embed.add_field(name="2️⃣ 직책 등급 제한",
        value="임베드 위→아래 순서가 높은→낮은 계급.\n본인 직책보다 높거나 같은 등급 처리 불가.\n(예: 참모장 → 사령관·부사령관 처리 불가)", inline=False)
    embed.add_field(name="3️⃣ 예하부대 직책 구분",
        value="자동완성에서 '제9공수특전여단 참모장' 처럼 부대명이 앞에 붙어 표시됩니다.\n같은 이름(참모장·주임원사 등)이 겹쳐도 정확한 줄을 찾아 처리합니다.", inline=False)
    embed.add_field(name="4️⃣ ★ 계급 제한",
        value="직책의 별 계급보다 본인이 반드시 높아야 합니다.\n준장(1★) → 장성급 직책 처리 불가", inline=False)
    embed.add_field(name="5️⃣ 셀프 처리 금지", value="본인을 직접 임명·해임할 수 없습니다.", inline=False)
    embed.set_footer(text="RTC 국방인사정보체계 | 인사 명령은 정확히 입력해 주세요.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────
# /인사조회
# ──────────────────────────────────────────────


@bot.tree.command(name="인사조회", description="특정 유저가 현재 맡고 있는 직책을 전군에서 조회합니다.")
@app_commands.describe(대상자="조회할 유저를 선택하세요. 생략하면 본인을 조회합니다.")
async def personnel_lookup(
    interaction: discord.Interaction,
    대상자: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    target = 대상자 if 대상자 is not None else interaction.user
    appointments = load_json(APPOINTMENTS_PATH, {})

    # 전 부대에서 target.mention 을 가진 직책 수집
    results: list[tuple[str, str]] = []  # [(unit, pos_value), ...]
    for unit, pos_dict in appointments.items():
        for pos_value, mention in pos_dict.items():
            if str(target.id) in mention:
                results.append((unit, pos_value))

    is_self = target.id == interaction.user.id
    subject = "본인" if is_self else target.display_name

    if not results:
        embed = discord.Embed(
            title=f"🔍 인사 조회 — {target.display_name}",
            description=(
                f"{target.mention} 은(는) 현재 조직도에 등록된 직책이 없습니다.\n"
                "공석이거나 아직 `/임명`이 실행되지 않은 상태입니다."
            ),
            color=0x2F3136,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="RTC 국방인사정보체계 | 인사 조회")
        embed.timestamp = datetime.now()
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # 소속별로 묶기
    by_unit: dict[str, list[str]] = {}
    for unit, pos_value in results:
        by_unit.setdefault(unit, []).append(display_pos(pos_value))

    color = 0x5865F2 if len(results) == 1 else 0xFFAA00

    embed = discord.Embed(
        title=f"🔍 인사 조회 — {target.display_name}",
        description=(
            f"{target.mention} 의 현재 직책 현황입니다.\n"
            f"총 **{len(results)}개** 직책을 보유하고 있습니다."
        ),
        color=color,
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    for unit, pos_names in by_unit.items():
        lines = [f"🎖️ `{p}`" for p in pos_names]
        embed.add_field(
            name=f"🏛️ {unit}",
            value="\n".join(lines),
            inline=True,
        )

    # 서버 내 역할 표시 (조직도와 관련된 역할만)
    org_roles = [
        r for r in target.roles
        if any(unit in r.name or r.name in unit for unit in UNIT_NAMES)
        or any(kw in r.name for kw in ["장성급", "HeadAdmin", "국방부", "부소유자"])
    ]
    if org_roles:
        embed.add_field(
            name="📌 보유 역할",
            value=" ".join(f"`{r.name}`" for r in org_roles),
            inline=False,
        )

    embed.set_footer(text=f"RTC 국방인사정보체계 | {subject} 인사 조회")
    embed.timestamp = datetime.now()
    await interaction.followup.send(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────
# /도움말
# ──────────────────────────────────────────────


@bot.tree.command(name="도움말", description="RTC 국방인사정보체계 전체 명령어 목록을 출력합니다.")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 RTC 국방인사정보체계 명령어 목록", color=0x2F3136)
    embed.add_field(name="✅ /임명", value="공석 직책에 유저를 임명합니다. 소속 선택 시 직책 자동완성.", inline=False)
    embed.add_field(name="🗑️ /해임", value="직책을 공석으로 처리합니다. 소속 선택 시 직책 자동완성.", inline=False)
    embed.add_field(name="↩️ /임명취소",
        value="@대상자만: 전체 공석 처리 | 소속+직책명: 직전 상태로 복구", inline=False)
    embed.add_field(name="🔍 /인사조회 [@유저]",
        value="특정 유저(또는 본인)가 맡은 직책을 전군에서 조회합니다.", inline=False)
    embed.add_field(name="📋 /조직도생성 [임베드번호]",
        value="조직도 임베드를 채널에 게시합니다. (HeadAdmin 전용)", inline=False)
    embed.add_field(name="🔄 /조직도리셋",
        value="기존 조직도를 삭제하고 처음부터 재생성합니다. (HeadAdmin 전용)", inline=False)
    embed.add_field(name="🔍 /조직도확인 [소속]",
        value="전군 요약 또는 소속별 직책 임명 현황을 테이블로 확인합니다.", inline=False)
    embed.add_field(name="📖 /인사행정처리안내", value="명령어 역할, 권한 규칙 전체 안내를 출력합니다.", inline=False)
    embed.set_footer(text="RTC 국방인사정보체계 | 모든 명령어는 / (슬래시)로 사용합니다.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────
# !sync  (소유자 전용)
# ──────────────────────────────────────────────


@bot.command(name="sync")
async def manual_sync(ctx: commands.Context):
    if not is_owner(ctx.author):
        await ctx.send("❌ 소유자 전용 명령어입니다.", delete_after=5)
        return
    await ctx.send("🔄 명령어 동기화 중... (전역 명령어 중복 제거 포함)")
    total = await _sync_guilds_only()
    await ctx.send(
        f"✅ 슬래시 명령어 길드별 동기화 완료: **{total}개**\n"
        "Discord 캐시 반영까지 최대 1분 소요될 수 있습니다."
    )


bot.run(TOKEN)
