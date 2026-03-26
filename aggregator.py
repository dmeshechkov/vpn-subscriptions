#!/usr/bin/env python3
"""
VPN Config Aggregator - Собирает конфигурации из нескольких источников,
тестирует и оставляет топ-10 по пингу
"""

import asyncio
import aiohttp
import socket
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
import urllib.parse
import json
import base64

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vpn_updater.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Список источников подписок
SUBSCRIPTION_SOURCES = [
    "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist.txt",
    "https://raw.githubusercontent.com/zieng2/wl/main/vless_lite.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/subscriptions/cidr_mobile_1.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/subscriptions/cidr_mobile_2.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/subscriptions/cidr_full.txt",
]


@dataclass
class VPNConfig:
    """Класс для хранения информации о конфигурации"""
    raw: str
    protocol: str
    host: str
    port: int
    name: str
    source: str = ""
    is_alive: bool = False
    latency: float = float('inf')

    def __lt__(self, other):
        return self.latency < other.latency


class ConfigCollector:
    """Сбор конфигураций из разных источников"""

    def __init__(self):
        self.configs: List[VPNConfig] = []

    async def fetch_from_url(self, session: aiohttp.ClientSession, url: str) -> List[str]:
        """Загрузка конфигураций из URL"""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    text = await response.text()
                    lines = text.split('\n')
                    configs = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('#') and not line.startswith('//'):
                            # Пропускаем строки с мета-информацией
                            if any(line.startswith(x) for x in ['vless://', 'vmess://', 'ss://', 'trojan://']):
                                configs.append(line)
                    logger.info(f"Загружено {len(configs)} конфигураций из {url}")
                    return configs
                else:
                    logger.error(f"Ошибка загрузки {url}: {response.status}")
                    return []
        except Exception as e:
            logger.error(f"Ошибка при загрузке {url}: {e}")
            return []

    def parse_config(self, line: str, source: str) -> Optional[VPNConfig]:
        """Парсинг конфигурации"""
        line = line.strip()
        if not line:
            return None

        try:
            # VLESS протокол
            if line.startswith('vless://'):
                protocol = 'vless'
                parsed = urllib.parse.urlparse(line)
                host = parsed.hostname
                port = parsed.port

                # Извлекаем имя
                if parsed.fragment:
                    name = urllib.parse.unquote(parsed.fragment)
                else:
                    name = f"{host}:{port}"

                # Обрезаем слишком длинные имена
                if len(name) > 50:
                    name = name[:47] + "..."

                return VPNConfig(
                    raw=line,
                    protocol=protocol,
                    host=host,
                    port=port,
                    name=name,
                    source=source
                )

            # VMess протокол
            elif line.startswith('vmess://'):
                protocol = 'vmess'
                try:
                    decoded = base64.b64decode(line[8:]).decode('utf-8')
                    vmess_data = json.loads(decoded)
                    host = vmess_data.get('add', 'unknown')
                    port = vmess_data.get('port', 0)
                    name = vmess_data.get('ps', f"{host}:{port}")

                    return VPNConfig(
                        raw=line,
                        protocol=protocol,
                        host=host,
                        port=int(port),
                        name=name[:50],
                        source=source
                    )
                except Exception as e:
                    logger.debug(f"Ошибка парсинга VMess: {e}")
                    return None

        except Exception as e:
            logger.debug(f"Ошибка парсинга: {e}")
            return None

        return None

    async def collect_all(self) -> List[VPNConfig]:
        """Сбор конфигураций из всех источников"""
        all_configs = []
        seen = set()  # Для дедупликации

        async with aiohttp.ClientSession() as session:
            for source in SUBSCRIPTION_SOURCES:
                lines = await self.fetch_from_url(session, source)
                for line in lines:
                    config = self.parse_config(line, source)
                    if config:
                        # Дедупликация по хосту и порту
                        key = f"{config.host}:{config.port}"
                        if key not in seen:
                            seen.add(key)
                            all_configs.append(config)

        logger.info(f"Всего собрано уникальных конфигураций: {len(all_configs)}")
        return all_configs


class LatencyTester:
    """Тестирование задержки конфигураций"""

    def __init__(self, timeout: int = 5, test_count: int = 2):
        self.timeout = timeout
        self.test_count = test_count

    async def test_tcp_latency(self, host: str, port: int) -> Tuple[bool, float]:
        """Тестирование TCP задержки"""
        latencies = []

        for attempt in range(self.test_count):
            try:
                start = time.time()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self.timeout
                )
                latency = (time.time() - start) * 1000
                writer.close()
                await writer.wait_closed()
                latencies.append(latency)

                # Если пинг уже хороший, не ждем остальные
                if latency < 100:
                    break

            except asyncio.TimeoutError:
                return False, float('inf')
            except Exception as e:
                return False, float('inf')

        if latencies:
            # Берем минимальный пинг
            min_latency = min(latencies)
            return True, min_latency
        else:
            return False, float('inf')

    async def test_config(self, config: VPNConfig) -> VPNConfig:
        """Тестирование одной конфигурации"""
        alive, latency = await self.test_tcp_latency(config.host, config.port)

        if alive:
            config.is_alive = True
            config.latency = latency
            logger.info(f"✅ {config.name[:40]} - {latency:.0f}ms ({config.source[:30]})")
        else:
            logger.debug(f"❌ {config.name[:40]} - не работает")

        return config

    async def test_all(self, configs: List[VPNConfig], max_concurrent: int = 50) -> List[VPNConfig]:
        """Тестирование всех конфигураций с параллельностью"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def test_with_semaphore(config):
            async with semaphore:
                return await self.test_config(config)

        tasks = [test_with_semaphore(c) for c in configs]
        results = await asyncio.gather(*tasks)

        working = [c for c in results if c.is_alive]
        working.sort()

        logger.info(f"Проверено {len(configs)} конфигураций, найдено {len(working)} рабочих")
        return working


class SubscriptionBuilder:
    """Создание подписки с топ-10 конфигурациями"""

    def __init__(self, output_dir: str = "subscriptions"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

    def build_subscription(self, configs: List[VPNConfig], max_count: int = 10) -> Path:
        """Создание файла подписки с топ-N конфигураций"""
        top_configs = configs[:max_count]

        filepath = self.output_dir / "whitelist_top10.txt"

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# profile-title: 🏳️ WhiteList VPN - Top 10\n")
            f.write("# profile-update-interval: 1\n")
            f.write(f"# Date/Time: {datetime.now().strftime('%Y-%m-%d / %H:%M')}\n")
            f.write(f"# Total tested: {len(configs)}\n")
            f.write(f"# Working: {len(top_configs)}\n")
            if top_configs:
                f.write(f"# Best latency: {top_configs[0].latency:.0f}ms\n")
            f.write("# Auto-updated every hour\n")
            f.write("\n")

            # Добавляем комментарии с пингом для удобства
            for i, config in enumerate(top_configs, 1):
                f.write(f"# {i}. {config.name} - {config.latency:.0f}ms\n")
                f.write(f"{config.raw}\n")

        logger.info(f"✅ Создана подписка: {filepath} (топ {len(top_configs)} из {len(configs)})")
        return filepath

    def create_readme(self, configs: List[VPNConfig], top_configs: List[VPNConfig]):
        """Создание README с результатами"""
        filepath = self.output_dir / "README.md"

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# 🏳️ WhiteList VPN - Top 10 Configurations\n\n")
            f.write(f"**Last update:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("## 📊 Statistics\n\n")
            f.write(f"- Total tested: **{len(configs)}**\n")
            f.write(f"- Working: **{len(top_configs)}**\n")
            f.write(f"- Failed: **{len(configs) - len(top_configs)}**\n\n")

            f.write("## 🏆 Top 10 by Latency\n\n")
            f.write("| # | Name | Latency | Source |\n")
            f.write("|---|------|---------|--------|\n")
            for i, config in enumerate(top_configs[:10], 1):
                source_short = config.source.split('/')[-1][:20] if config.source else "unknown"
                f.write(f"| {i} | {config.name[:40]} | {config.latency:.0f}ms | {source_short} |\n")

            f.write("\n## 🔗 Subscription Link\n\n")
            f.write(
                "```\nhttps://raw.githubusercontent.com/dmeshechkov/vpn-subscriptions/main/subscriptions/whitelist_top10.txt\n```\n")

            f.write("\n## 📱 How to Use\n\n")
            f.write("1. Copy the subscription link above\n")
            f.write("2. Open your VPN client (v2rayN, Karing, Streisand, etc.)\n")
            f.write("3. Add new subscription and paste the link\n")
            f.write("4. Set auto-update interval to 1 hour\n")
            f.write("5. Enjoy the fastest connections!\n")


async def main():
    """Основная функция"""
    parser = argparse.ArgumentParser(description='VPN Config Aggregator - Top 10 by latency')
    parser.add_argument('--max-configs', type=int, default=10, help='Максимум конфигураций в подписке')
    parser.add_argument('--output-dir', '-o', default='subscriptions', help='Директория для вывода')
    parser.add_argument('--timeout', type=int, default=5, help='Таймаут проверки в секундах')
    parser.add_argument('--threads', type=int, default=50, help='Количество параллельных проверок')
    args = parser.parse_args()

    start_time = time.time()
    logger.info("🚀 Начинаем сбор и проверку конфигураций...")

    # Шаг 1: Сбор конфигураций
    collector = ConfigCollector()
    all_configs = await collector.collect_all()

    if not all_configs:
        logger.error("Не найдено конфигураций!")
        return

    # Шаг 2: Тестирование задержки
    tester = LatencyTester(timeout=args.timeout)
    working_configs = await tester.test_all(all_configs, max_concurrent=args.threads)

    if not working_configs:
        logger.error("Не найдено рабочих конфигураций!")
        return

    # Шаг 3: Создание подписки с топ-10
    builder = SubscriptionBuilder(args.output_dir)
    subscription_file = builder.build_subscription(working_configs, max_count=args.max_configs)
    builder.create_readme(all_configs, working_configs)

    # Вывод результатов
    elapsed = time.time() - start_time

    print("\n" + "=" * 60)
    print("📊 РЕЗУЛЬТАТЫ ПРОВЕРКИ")
    print("=" * 60)
    print(f"📥 Всего собрано: {len(all_configs)} конфигураций")
    print(f"✅ Рабочих: {len(working_configs)}")
    print(f"❌ Нерабочих: {len(all_configs) - len(working_configs)}")
    print(f"⏱️  Время проверки: {elapsed:.1f} секунд")
    print(f"\n🏆 ТОП-{min(args.max_configs, len(working_configs))} ПО ПИНГУ:")

    for i, config in enumerate(working_configs[:args.max_configs], 1):
        print(f"   {i}. {config.name[:45]} - {config.latency:.0f}ms")

    print(f"\n📁 Файлы сохранены в: {builder.output_dir}/")
    print(f"   - whitelist_top10.txt (подписка для импорта)")
    print(f"   - README.md (статистика)")
    print(f"\n🔗 Ссылка для импорта в VPN клиент:")
    print(f"   https://raw.githubusercontent.com/dmeshechkov/vpn-subscriptions/main/subscriptions/whitelist_top10.txt")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())