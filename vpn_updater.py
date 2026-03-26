#!/usr/bin/env python3
"""
VPN Config Validator & Subscription Updater
Автоматическая проверка конфигураций и создание подписок
"""

import asyncio
import aiohttp
import json
import re
import subprocess
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse

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
    speed: float = 0.0

class VPNConfigValidator:
    """Класс для проверки работоспособности VPN конфигураций"""
    
    def __init__(self, timeout: int = 10, test_url: str = "https://www.google.com"):
        self.timeout = timeout
        self.test_url = test_url
        self.results: List[VPNConfig] = []
        
    def parse_config(self, line: str) -> Optional[VPNConfig]:
        """Парсинг строки конфигурации"""
        line = line.strip()
        if not line or line.startswith('#'):
            return None
            
        try:
            # Определяем протокол
            if line.startswith('vless://'):
                protocol = 'vless'
                # Парсим URL
                parsed = urllib.parse.urlparse(line)
                host = parsed.hostname
                port = parsed.port
                # Извлекаем имя из фрагмента (#)
                name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else f"{host}:{port}"
            elif line.startswith('vmess://'):
                protocol = 'vmess'
                # Для VMess нужно декодировать base64
                try:
                    import base64
                    decoded = base64.b64decode(line[8:]).decode('utf-8')
                    vmess_data = json.loads(decoded)
                    host = vmess_data.get('add', 'unknown')
                    port = vmess_data.get('port', 0)
                    name = vmess_data.get('ps', f"{host}:{port}")
                except:
                    return None
            elif line.startswith('ss://'):
                protocol = 'shadowsocks'
                host = 'unknown'
                port = 0
                name = 'Shadowsocks'
            else:
                return None
                
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
    
    async def test_tcp_connection(self, host: str, port: int) -> Tuple[bool, float]:
        """Тестирование TCP подключения"""
        try:
            start_time = time.time()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=self.timeout
            )
            latency = (time.time() - start_time) * 1000  # в миллисекундах
            writer.close()
            await writer.wait_closed()
            return True, latency
        except Exception as e:
            logger.debug(f"TCP тест не удался для {host}:{port} - {e}")
            return False, float('inf')
    
    async def test_http_proxy(self, config: VPNConfig, proxy_url: str) -> Tuple[bool, float]:
        """Тестирование через HTTP прокси"""
        try:
            # Создаем сессию с прокси
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                start_time = time.time()
                async with session.get(
                    self.test_url,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    latency = (time.time() - start_time) * 1000
                    return response.status == 200, latency
        except Exception as e:
            logger.debug(f"HTTP тест не удался для {config.name} - {e}")
            return False, float('inf')
    
    async def test_config(self, config: VPNConfig) -> VPNConfig:
        """Тестирование отдельной конфигурации"""
        # Сначала тестируем TCP подключение
        tcp_ok, latency = await self.test_tcp_connection(config.host, config.port)
        
        if tcp_ok:
            config.is_alive = True
            config.latency = latency
            logger.debug(f"✅ {config.name} - работает (latency: {latency:.2f}ms)")
        else:
            logger.debug(f"❌ {config.name} - не работает")
            
        return config
    
    async def validate_configs(self, configs: List[VPNConfig], max_concurrent: int = 50) -> List[VPNConfig]:
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
        working.sort(key=lambda x: x.latency)
        
        logger.info(f"Проверено {len(configs)} конфигураций, найдено {len(working)} рабочих")
        return working

class SubscriptionManager:
    """Класс для управления подписками"""
    
    def __init__(self, output_dir: str = "subscriptions"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
    def create_subscription_file(self, configs: List[VPNConfig], filename: str, 
                                 title: str, max_count: int = None) -> Path:
        """Создание файла подписки"""
        if max_count:
            configs = configs[:max_count]
            
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            # Заголовок подписки
            f.write(f"# profile-title: {title}\n")
            f.write(f"# profile-update-interval: 5\n")
            f.write(f"# Date/Time: {datetime.now().strftime('%Y-%m-%d / %H:%M')}\n")
            f.write(f"# Количество: {len(configs)}\n")
            f.write("\n")
            
            # Конфигурации
            for config in configs:
                f.write(f"{config.raw}\n")
                
        logger.info(f"Создана подписка: {filepath} ({len(configs)} конфигураций)")
        return filepath
    
    def generate_qr_code(self, content: str, filename: str):
        """Генерация QR кода для подписки (опционально)"""
        try:
            import qrcode
            img = qrcode.make(content)
            img.save(self.output_dir / filename)
            logger.info(f"QR код создан: {filename}")
        except ImportError:
            logger.warning("Модуль qrcode не установлен. Установите: pip install qrcode[pil]")
    
    def create_readme(self, subscriptions: Dict[str, Dict]):
        """Создание README файла со списком подписок"""
        readme_path = self.output_dir / "README.md"
        
        with open(readme_path, 'w', encoding='utf-8') as f:
            f.write("# VPN Subscriptions\n\n")
            f.write(f"Последнее обновление: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("## Доступные подписки\n\n")
            
            for name, info in subscriptions.items():
                f.write(f"### {name}\n")
                f.write(f"- **Описание:** {info['description']}\n")
                f.write(f"- **Количество:** {info['count']}\n")
                f.write(f"- **Ссылка:** `{info['url']}`\n\n")
                
        logger.info(f"Создан README файл: {readme_path}")

class ConfigCollector:
    """Класс для сбора конфигураций из разных источников"""
    
    def __init__(self):
        self.configs: List[VPNConfig] = []
        
    def load_from_file(self, filepath: str) -> List[str]:
        """Загрузка конфигураций из файла"""
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return [line.strip() for line in lines if line.strip() and not line.startswith('#')]
    
    def load_from_url(self, url: str) -> List[str]:
        """Загрузка конфигураций из URL"""
        import requests
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                return [line.strip() for line in response.text.split('\n') 
                       if line.strip() and not line.startswith('#')]
        except Exception as e:
            logger.error(f"Ошибка загрузки из {url}: {e}")
        return []
    
    def filter_by_country(self, configs: List[VPNConfig], country: str) -> List[VPNConfig]:
        """Фильтрация по стране"""
        return [c for c in configs if country.lower() in c.name.lower()]
    
    def filter_by_cidr(self, configs: List[VPNConfig]) -> List[VPNConfig]:
        """Фильтрация для белых списков CIDR"""
        # Здесь можно добавить логику фильтрации по CIDR диапазонам
        return configs

class GitHubDeployer:
    """Класс для деплоя на GitHub"""
    
    def __init__(self, repo_path: str = "."):
        self.repo_path = Path(repo_path)
        
    def commit_and_push(self, message: str, files: List[Path]):
        """Коммит и пуш изменений в GitHub"""
        try:
            # Добавляем файлы
            for file in files:
                subprocess.run(['git', 'add', str(file)], cwd=self.repo_path, check=True)
            
            # Коммитим
            subprocess.run(['git', 'commit', '-m', message], cwd=self.repo_path, check=True)
            
            # Пушим
            subprocess.run(['git', 'push'], cwd=self.repo_path, check=True)
            
            logger.info("Изменения успешно отправлены в GitHub")
        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка при деплое: {e}")

async def main():
    """Основная функция"""
    parser = argparse.ArgumentParser(description='VPN Config Validator & Updater')
    parser.add_argument('--input', '-i', help='Входной файл с конфигурациями')
    parser.add_argument('--url', '-u', help='URL для загрузки конфигураций')
    parser.add_argument('--max-configs', type=int, default=150, help='Максимум конфигураций в подписке')
    parser.add_argument('--output-dir', '-o', default='subscriptions', help='Директория для вывода')
    parser.add_argument('--deploy', action='store_true', help='Автоматический деплой на GitHub')
    args = parser.parse_args()
    
    # Сбор конфигураций
    collector = ConfigCollector()
    raw_configs = []
    
    if args.input:
        logger.info(f"Загрузка конфигураций из файла: {args.input}")
        raw_configs = collector.load_from_file(args.input)
    elif args.url:
        logger.info(f"Загрузка конфигураций из URL: {args.url}")
        raw_configs = collector.load_from_url(args.url)
    else:
        logger.error("Необходимо указать --input или --url")
        return
    
    logger.info(f"Загружено {len(raw_configs)} конфигураций")
    
    # Парсинг конфигураций
    validator = VPNConfigValidator()
    configs = []
    for raw in raw_configs:
        config = validator.parse_config(raw)
        if config:
            configs.append(config)
    
    logger.info(f"Распознано {len(configs)} конфигураций")
    
    # Проверка работоспособности
    logger.info("Начинаем проверку конфигураций...")
    working_configs = await validator.validate_configs(configs, max_concurrent=50)
    
    if not working_configs:
        logger.error("Не найдено рабочих конфигураций!")
        return
    
    # Создание подписок
    manager = SubscriptionManager(args.output_dir)
    subscriptions = {}
    
    # Полная подписка
    full_file = manager.create_subscription_file(
        working_configs,
        "full_subscription.txt",
        "Full VPN Subscription",
        max_count=args.max_configs
    )
    subscriptions['Full'] = {
        'description': 'Полная подписка с лучшими конфигурациями',
        'count': min(len(working_configs), args.max_configs),
        'url': str(full_file)
    }
    
    # Подписка для телефона (первые 150)
    if len(working_configs) > 150:
        mobile_file = manager.create_subscription_file(
            working_configs,
            "mobile_subscription.txt",
            "Mobile VPN Subscription (Top 150)",
            max_count=150
        )
        subscriptions['Mobile'] = {
            'description': 'Сжатая подписка для телефона (первые 150)',
            'count': 150,
            'url': str(mobile_file)
        }
    
    # Фильтрация по протоколам
    vless_configs = [c for c in working_configs if c.protocol == 'vless']
    if vless_configs:
        vless_file = manager.create_subscription_file(
            vless_configs,
            "vless_subscription.txt",
            "VLESS Only Subscription",
            max_count=args.max_configs
        )
        subscriptions['VLESS'] = {
            'description': 'Только VLESS протокол',
            'count': min(len(vless_configs), args.max_configs),
            'url': str(vless_file)
        }
    
    # Создание README
    manager.create_readme(subscriptions)
    
    # Деплой на GitHub
    if args.deploy:
        deployer = GitHubDeployer()
        files = list(manager.output_dir.glob('*'))
        deployer.commit_and_push(
            f"Auto-update subscriptions {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            files
        )
    
    logger.info("Готово!")

if __name__ == "__main__":
    asyncio.run(main())
