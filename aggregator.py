#!/usr/bin/env python3
"""
VPN Config Aggregator - Собирает, тестирует и обновляет топ-15 конфигураций на GitHub
Запуск: python aggregator.py [--max-configs 15] [--timeout 5] [--push]
"""

import asyncio
import aiohttp
import socket
import time
import logging
import argparse
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional
from dataclasses import dataclass
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
    async def fetch_from_url(self, session: aiohttp.ClientSession, url: str) -> List[str]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    text = await response.text()
                    configs = []
                    for line in text.split('\n'):
                        line = line.strip()
                        if line and not line.startswith('#') and not line.startswith('//'):
                            if any(line.startswith(x) for x in ['vless://', 'vmess://', 'ss://', 'trojan://']):
                                configs.append(line)
                    return configs
                return []
        except Exception as e:
            logger.error(f"Ошибка загрузки {url}: {e}")
            return []

    def parse_config(self, line: str, source: str) -> Optional[VPNConfig]:
        if not line.startswith('vless://'):
            return None
        try:
            parsed = urllib.parse.urlparse(line)
            host = parsed.hostname
            port = parsed.port
            name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"{host}:{port}"
            if len(name) > 50:
                name = name[:47] + "..."
            return VPNConfig(raw=line, protocol='vless', host=host, port=port, name=name, source=source)
        except:
            return None

    async def collect_all(self) -> List[VPNConfig]:
        all_configs = []
        seen = set()
        async with aiohttp.ClientSession() as session:
            for source in SUBSCRIPTION_SOURCES:
                lines = await self.fetch_from_url(session, source)
                for line in lines:
                    config = self.parse_config(line, source)
                    if config:
                        key = f"{config.host}:{config.port}"
                        if key not in seen:
                            seen.add(key)
                            all_configs.append(config)
        logger.info(f"Собрано уникальных конфигураций: {len(all_configs)}")
        return all_configs


class LatencyTester:
    def __init__(self, timeout: int = 5):
        self.timeout = timeout

    async def test_tcp_latency(self, host: str, port: int) -> Tuple[bool, float]:
        try:
            start = time.time()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self.timeout
            )
            latency = (time.time() - start) * 1000
            writer.close()
            await writer.wait_closed()
            return True, latency
        except:
            return False, float('inf')

    async def test_config(self, config: VPNConfig) -> VPNConfig:
        alive, latency = await self.test_tcp_latency(config.host, config.port)
        if alive:
            config.is_alive = True
            config.latency = latency
        return config

    async def test_all(self, configs: List[VPNConfig], max_concurrent: int = 50) -> List[VPNConfig]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def test_with_semaphore(config):
            async with semaphore:
                return await self.test_config(config)

        tasks = [test_with_semaphore(c) for c in configs]
        results = await asyncio.gather(*tasks)
        working = [c for c in results if c.is_alive]
        working.sort()
        logger.info(f"Проверено {len(configs)}, рабочих: {len(working)}")
        return working


class GitHubUpdater:
    def __init__(self, repo_path: str = "."):
        self.repo_path = Path(repo_path)

    def commit_and_push(self, message: str) -> bool:
        try:
            subprocess.run(['git', 'add', 'subscriptions/'], cwd=self.repo_path, check=True, capture_output=True)
            result = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=self.repo_path)
            if result.returncode == 0:
                logger.info("Нет изменений для коммита")
                return True
            subprocess.run(['git', 'commit', '-m', message], cwd=self.repo_path, check=True, capture_output=True)
            subprocess.run(['git', 'push'], cwd=self.repo_path, check=True, capture_output=True)
            logger.info(f"✅ Изменения отправлены на GitHub: {message}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка Git: {e}")
            return False

    def create_subscription_file(self, configs: List[VPNConfig], max_count: int = 15) -> Path:
        top_configs = configs[:max_count]
        filepath = self.repo_path / "subscriptions" / "whitelist_top15.txt"
        filepath.parent.mkdir(exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# profile-title: 🏳️ WhiteList VPN - Top 15\n")
            f.write("# profile-update-interval: 1\n")
            f.write(f"# Date/Time: {datetime.now().strftime('%Y-%m-%d / %H:%M')}\n")
            f.write(f"# Total tested: {len(configs)}\n")
            f.write(f"# Working: {len(top_configs)}\n")
            if top_configs:
                f.write(f"# Best latency: {top_configs[0].latency:.0f}ms\n")
            f.write("# Auto-updated every hour\n\n")
            for i, config in enumerate(top_configs, 1):
                f.write(f"# {i}. {config.name} - {config.latency:.0f}ms\n")
                f.write(f"{config.raw}\n")
        logger.info(f"✅ Создан файл: {filepath}")
        return filepath


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-configs', type=int, default=15)
    parser.add_argument('--timeout', type=int, default=5)
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()

    start = time.time()
    logger.info("🚀 Сбор и проверка конфигураций...")

    collector = ConfigCollector()
    all_configs = await collector.collect_all()
    if not all_configs:
        logger.error("Нет конфигураций")
        sys.exit(1)

    tester = LatencyTester(timeout=args.timeout)
    working = await tester.test_all(all_configs)

    if not working:
        logger.error("Нет рабочих конфигураций")
        sys.exit(1)

    updater = GitHubUpdater()
    updater.create_subscription_file(working, max_count=args.max_configs)

    print(f"\n✅ Рабочих: {len(working)}")
    print(f"🏆 Топ-{args.max_configs}:")
    for i, c in enumerate(working[:args.max_configs], 1):
        print(f"   {i}. {c.name[:40]} - {c.latency:.0f}ms")

    if args.push:
        print("\n📤 Отправка на GitHub...")
        updater.commit_and_push(
            f"Auto-update: top {args.max_configs} configs ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    else:
        print("\n💡 Для отправки на GitHub добавьте --push")

    print(
        f"\n🔗 Подписка: https://raw.githubusercontent.com/dmeshechkov/vpn-subscriptions/main/subscriptions/whitelist_top15.txt")


if __name__ == "__main__":
    asyncio.run(main())