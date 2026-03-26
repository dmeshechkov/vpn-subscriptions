#!/usr/bin/env python3
"""
VPN Config Validator & Subscription Updater
Проверяет конфигурации и оставляет только 10 с наименьшим пингом
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
from concurrent.futures import ThreadPoolExecutor
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


@dataclass
class VPNConfig:
    """Класс для хранения информации о конфигурации"""
    raw: str
    protocol: str
    host: str
    port: int
    name: str
    is_alive: bool = False
    latency: float = float('inf')
    country: str = "Unknown"

    def __lt__(self, other):
        return self.latency < other.latency


class VPNConfigValidator:
    """Класс для проверки работоспособности VPN конфигураций"""

    def __init__(self, timeout: int = 5, test_count: int = 3):
        self.timeout = timeout
        self.test_count = test_count  # Количество попыток для усреднения

    def parse_config(self, line: str) -> Optional[VPNConfig]:
        """Парсинг строки конфигурации"""
        line = line.strip()
        if not line or line.startswith('#'):
            return None

        try:
            # VLESS протокол
            if line.startswith('vless://'):
                protocol = 'vless'
                parsed = urllib.parse.urlparse(line)
                host = parsed.hostname
                port = parsed.port

                # Извлекаем имя из фрагмента (#)
                if parsed.fragment:
                    name = urllib.parse.unquote(parsed.fragment)
                else:
                    # Пытаемся извлечь из параметров
                    name = f"{host}:{port}"

                return VPNConfig(
                    raw=line,
                    protocol=protocol,
                    host=host,
                    port=port,
                    name=name
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
                        name=name
                    )
                except Exception as e:
                    logger.debug(f"Ошибка парсинга VMess: {e}")
                    return None

            # Shadowsocks протокол
            elif line.startswith('ss://'):
                protocol = 'shadowsocks'
                # Простой парсинг SS
                parts = line.split('@')
                if len(parts) > 1:
                    host_port = parts[1].split('#')[0].split(':')
                    if len(host_port) >= 2:
                        host = host_port[0]
                        port = int(host_port[1])
                        name = line.split('#')[-1] if '#' in line else f"{host}:{port}"
                    else:
                        return None
                else:
                    return None

                return VPNConfig(
                    raw=line,
                    protocol=protocol,
                    host=host,
                    port=port,
                    name=name
                )

            # Trojan протокол
            elif line.startswith('trojan://'):
                protocol = 'trojan'
                parsed = urllib.parse.urlparse(line)
                host = parsed.hostname
                port = parsed.port
                name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"{host}:{port}"

                return VPNConfig(
                    raw=line,
                    protocol=protocol,
                    host=host,
                    port=port,
                    name=name
                )

        except Exception as e:
            logger.debug(f"Ошибка парсинга конфигурации: {e}")
            return None

        return None

    async def test_tcp_connection(self, host: str, port: int) -> Tuple[bool, float]:
        """Тестирование TCP подключения с усреднением"""
        latencies = []

        for attempt in range(self.test_count):
            try:
                start_time = time.time()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self.timeout
                )
                latency = (time.time() - start_time) * 1000  # в миллисекундах
                writer.close()
                await writer.wait_closed()
                latencies.append(latency)

                # Если первый пинг уже хороший, не ждем остальные
                if latency < 100:
                    break

            except asyncio.TimeoutError:
                logger.debug(f"Таймаут для {host}:{port}")
                return False, float('inf')
            except Exception as e:
                logger.debug(f"Ошибка подключения к {host}:{port} - {e}")
                return False, float('inf')

        if latencies:
            # Берем медианное значение (более устойчиво к выбросам)
            latencies.sort()
            avg_latency = sum(latencies) / len(latencies)
            return True, avg_latency
        else:
            return False, float('inf')

    async def test_config(self, config: VPNConfig) -> VPNConfig:
        """Тестирование отдельной конфигурации"""
        tcp_ok, latency = await self.test_tcp_connection(config.host, config.port)

        if tcp_ok:
            config.is_alive = True
            config.latency = latency
            logger.info(f"✅ {config.name} - {latency:.0f}ms")
        else:
            logger.debug(f"❌ {config.name} - не работает")

        return config

    async def validate_configs(self, configs: List[VPNConfig], max_concurrent: int = 30) -> List[VPNConfig]:
        """Проверка списка конфигураций с ограничением параллельности"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def test_with_semaphore(config):
            async with semaphore:
                return await self.test_config(config)

        tasks = [test_with_semaphore(config) for config in configs]
        results = await asyncio.gather(*tasks)

        # Фильтруем только рабочие
        working = [r for r in results if r.is_alive]
        # Сортируем по задержке
        working.sort()

        logger.info(f"Проверено {len(configs)} конфигураций, найдено {len(working)} рабочих")
        return working


class SubscriptionManager:
    """Класс для управления подписками"""

    def __init__(self, output_dir: str = "subscriptions"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

    def create_subscription_file(self, configs: List[VPNConfig], filename: str,
                                 title: str, max_count: int = 10) -> Path:
        """Создание файла подписки с топ N конфигураций"""
        # Берем только лучшие (с наименьшим пингом)
        top_configs = configs[:max_count]

        filepath = self.output_dir / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            # Заголовок подписки
            f.write(f"# profile-title: {title}\n")
            f.write(f"# profile-update-interval: 5\n")
            f.write(f"# Date/Time: {datetime.now().strftime('%Y-%m-%d / %H:%M')}\n")
            f.write(f"# Total tested: {len(configs)}\n")
            f.write(f"# Working: {len(top_configs)}\n")
            f.write(f"# Best latency: {top_configs[0].latency:.0f}ms\n" if top_configs else "# No working configs\n")
            f.write("\n")

            # Конфигурации с комментариями о пинге
            for i, config in enumerate(top_configs, 1):
                f.write(f"# {i}. {config.name} - {config.latency:.0f}ms\n")
                f.write(f"{config.raw}\n")

        logger.info(f"Создана подписка: {filepath} (топ {len(top_configs)} из {len(configs)} конфигураций)")
        return filepath

    def create_detailed_report(self, configs: List[VPNConfig], filename: str = "report.txt") -> Path:
        """Создание детального отчета о всех конфигурациях"""
        filepath = self.output_dir / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write("VPN CONFIGURATIONS TEST REPORT\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n\n")

            f.write(f"Total tested: {len(configs)}\n")
            working = [c for c in configs if c.is_alive]
            f.write(f"Working: {len(working)}\n")
            f.write(f"Failed: {len(configs) - len(working)}\n\n")

            if working:
                f.write("TOP 10 BEST CONFIGURATIONS (by latency):\n")
                f.write("-" * 40 + "\n")
                for i, config in enumerate(working[:10], 1):
                    f.write(f"{i}. {config.name}\n")
                    f.write(f"   Protocol: {config.protocol}\n")
                    f.write(f"   Host: {config.host}:{config.port}\n")
                    f.write(f"   Latency: {config.latency:.0f}ms\n\n")

            f.write("\nALL WORKING CONFIGURATIONS:\n")
            f.write("-" * 40 + "\n")
            for config in working:
                f.write(f"{config.name} - {config.latency:.0f}ms\n")

        return filepath


async def main():
    """Основная функция"""
    parser = argparse.ArgumentParser(description='VPN Config Validator - Top 10 by latency')
    parser.add_argument('--input', '-i', required=True, help='Входной файл с конфигурациями')
    parser.add_argument('--max-configs', type=int, default=10,
                        help='Максимум конфигураций в подписке (по умолчанию 10)')
    parser.add_argument('--output-dir', '-o', default='subscriptions', help='Директория для вывода')
    parser.add_argument('--timeout', type=int, default=5, help='Таймаут проверки в секундах')
    parser.add_argument('--threads', type=int, default=30, help='Количество параллельных проверок')
    args = parser.parse_args()

    start_time = time.time()

    # Загрузка конфигураций
    logger.info(f"Загрузка конфигураций из {args.input}")
    with open(args.input, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Парсинг конфигураций
    validator = VPNConfigValidator(timeout=args.timeout)
    configs = []
    for line in lines:
        config = validator.parse_config(line)
        if config:
            configs.append(config)

    logger.info(f"Распознано {len(configs)} конфигураций")

    if not configs:
        logger.error("Не найдено валидных конфигураций!")
        return

    # Проверка работоспособности
    logger.info(f"Начинаем проверку {len(configs)} конфигураций (таймаут: {args.timeout}с)...")
    logger.info("Это может занять некоторое время...")

    working_configs = await validator.validate_configs(configs, max_concurrent=args.threads)

    if not working_configs:
        logger.error("Не найдено рабочих конфигураций!")
        return

    # Создание подписок
    manager = SubscriptionManager(args.output_dir)

    # Основная подписка с топ 10
    main_file = manager.create_subscription_file(
        working_configs,
        "full_subscription.txt",
        f"Top {args.max_configs} VPN Subscriptions (Auto-updated)",
        max_count=args.max_configs
    )

    # Детальный отчет
    report_file = manager.create_detailed_report(working_configs)

    # Создаем также файл с последней версией (для удобства)
    latest_file = manager.output_dir / "latest_subscription.txt"
    latest_file.write_text(main_file.read_text(encoding='utf-8'), encoding='utf-8')

    elapsed_time = time.time() - start_time

    # Вывод результатов
    print("\n" + "=" * 60)
    print("✅ ПРОВЕРКА ЗАВЕРШЕНА")
    print("=" * 60)
    print(f"📊 Всего проверено: {len(configs)} конфигураций")
    print(f"✅ Рабочих: {len(working_configs)}")
    print(f"❌ Нерабочих: {len(configs) - len(working_configs)}")
    print(f"⏱️  Время проверки: {elapsed_time:.1f} секунд")
    print(f"\n🏆 ТОП-{min(args.max_configs, len(working_configs))} ПО ПИНГУ:")

    for i, config in enumerate(working_configs[:args.max_configs], 1):
        print(f"   {i}. {config.name} - {config.latency:.0f}ms")

    print(f"\n📁 Файлы сохранены в: {manager.output_dir}/")
    print(f"   - full_subscription.txt (топ {args.max_configs})")
    print(f"   - latest_subscription.txt (последняя версия)")
    print(f"   - report.txt (детальный отчет)")

    # GitHub RAW ссылка
    print(f"\n🔗 GitHub RAW ссылка для импорта:")
    print(
        f"   https://raw.githubusercontent.com/dmeshechkov/vpn-subscriptions/main/subscriptions/full_subscription.txt")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())