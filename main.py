import asyncio
import logging
import sys
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from config import config
from database import db
from api_client import ToeApiClient
from notifier import NotifierService
from handlers import router
from keyboards import set_bot_username

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot-light")


async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="status", description="💡 Поточний стан світла"),
        BotCommand(command="today", description="📅 Графік на сьогодні"),
        BotCommand(command="tomorrow", description="📆 Графік на завтра"),
        BotCommand(command="accidents", description="🚨 Аварійні та ремонтні роботи"),
        BotCommand(command="settings", description="⚙️ Налаштування сповіщень"),
        BotCommand(command="stop", description="🔕 Призупинити сповіщення"),
        BotCommand(command="help", description="ℹ️ Довідка та допомога"),
    ]
    await bot.set_my_commands(commands)


async def keep_alive(service_url: str):
    health_url = f"{service_url.rstrip('/')}/health"
    logger.info(f"Keep-alive self-ping configured for: {health_url}")
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await asyncio.sleep(20)
        while True:
            try:
                async with session.get(
                    health_url, headers={"User-Agent": "BotLight-KeepAlive/1.0"}
                ) as resp:
                    logger.info(f"Keep-alive ping to {health_url}: status {resp.status}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Keep-alive ping error: {e}")
            await asyncio.sleep(120)


async def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error(
            "CRITICAL: BOT_TOKEN is not set!\n"
            "Please create a .env file and set BOT_TOKEN=your_token from @BotFather.\n"
            "Example file is available at .env.example"
        )
        sys.exit(1)

    logger.info("Initializing database...")
    await db.init()

    api_client = ToeApiClient()
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp["api_client"] = api_client
    dp.include_router(router)

    notifier_service = NotifierService(bot=bot, api_client=api_client)

    # Start lightweight health check HTTP server BEFORE Telegram network calls
    health_app = web.Application()
    health_app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
    health_app.router.add_get("/health", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info(f"Health check server listening on port {config.PORT}")

    bg_tasks: list[asyncio.Task] = []
    try:
        try:
            me = await bot.get_me()
            if me and me.username:
                set_bot_username(me.username)
                notifier_service.bot_username = me.username
                logger.info(f"Resolved bot username: @{me.username}")
        except Exception as e:
            logger.warning(f"Could not resolve bot username at startup: {e}")

        try:
            await set_commands(bot)
            logger.info("Bot commands registered.")
        except Exception as e:
            logger.warning(f"Could not register bot commands at startup: {e}")

        notifier_task = asyncio.create_task(notifier_service.start())
        bg_tasks.append(notifier_task)

        if config.RENDER_EXTERNAL_URL:
            keep_alive_task = asyncio.create_task(keep_alive(config.RENDER_EXTERNAL_URL))
            bg_tasks.append(keep_alive_task)

        logger.info(
            f"Starting bot for address: {config.CITY_NAME}, {config.formatted_street}, "
            f"{config.BUILDING_NAME} (queue {config.CHER_GPV})"
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Stopping bot and services...")
        notifier_service.stop()
        for task in bg_tasks:
            task.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        await api_client.close()
        await bot.session.close()
        await runner.cleanup()
        await db.close()
        logger.info("Bot successfully stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application terminated.")
