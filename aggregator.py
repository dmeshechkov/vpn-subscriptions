#!/usr/bin/env python3
"""
VPN Config Aggregator - Собирает, тестирует и обновляет топ-15 конфигураций на GitHub
Игнорирует аргументы командной строки для совместимости с GitHub Actions
"""

import requests
import socket
import time
import subprocess
import sys
from pathlib import Path
from datetime import datetime

# Игнорируем все аргументы командной строки (чтобы --push не вызывал ошибку)
if len(sys.argv) > 1:
    print(f"ℹ️ Ignoring arguments: {' '.join(sys.argv[1:])}")
    sys.argv = [sys.argv[0]]

print("=" * 60)
print("🚀 VPN SUBSCRIPTION UPDATER")
print("=" * 60)
print(f"📅 Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# Источники подписок
SOURCES = [
    "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist.txt",
    "https://raw.githubusercontent.com/zieng2/wl/main/vless_lite.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/subscriptions/cidr_mobile_1.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/subscriptions/cidr_mobile_2.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/subscriptions/cidr_full.txt",
]


def test_host(host: str, port: int, timeout: int = 3) -> tuple:
    """
    Проверяет доступность хоста и измеряет задержку
    Возвращает (доступен, задержка_в_ms)
    """
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        latency = (time.time() - start) * 1000
        sock.close()
        return True, latency
    except Exception:
        return False, None


def parse_vless(line: str) -> dict:
    """
    Парсит VLESS конфигурацию, извлекает хост, порт и имя
    """
    if not line.startswith('vless://'):
        return None
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(line)
        host = parsed.hostname
        port = parsed.port
        # Извлекаем имя из фрагмента (#)
        if parsed.fragment:
            name = urllib.parse.unquote(parsed.fragment)
        else:
            name = f"{host}:{port}"
        # Обрезаем слишком длинные имена
        if len(name) > 50:
            name = name[:47] + "..."
        return {
            'raw': line,
            'host': host,
            'port': port,
            'name': name
        }
    except Exception:
        return None


def collect_configs() -> list:
    """Собирает конфигурации из всех источников"""
    all_configs = []
    seen = set()  # Для дедупликации

    print("📥 COLLECTING CONFIGURATIONS")
    print("-" * 40)

    for source in SOURCES:
        try:
            print(f"  Loading from: {source.split('/')[-1]}...", end=" ")
            response = requests.get(source, timeout=30)
            if response.status_code == 200:
                count_before = len(all_configs)
                for line in response.text.split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#') and not line.startswith('//'):
                        config = parse_vless(line)
                        if config:
                            # Дедупликация по хосту и порту
                            key = f"{config['host']}:{config['port']}"
                            if key not in seen:
                                seen.add(key)
                                all_configs.append(config)
                added = len(all_configs) - count_before
                print(f"✅ {added} new configs")
            else:
                print(f"❌ HTTP {response.status_code}")
        except Exception as e:
            print(f"❌ Error: {e}")

    print(f"\n📊 Total unique configs collected: {len(all_configs)}")
    return all_configs


def test_configs(configs: list, max_to_test: int = 300) -> list:
    """
    Тестирует задержку конфигураций
    Возвращает список работающих, отсортированный по задержке
    """
    print("\n🏓 TESTING LATENCY")
    print("-" * 40)
    print(f"  Testing first {min(max_to_test, len(configs))} configs...")
    print()

    working = []

    for i, config in enumerate(configs[:max_to_test]):
        # Выводим прогресс
        print(f"  {i + 1:3d}. {config['name'][:40]:40s}...", end=" ", flush=True)

        alive, latency = test_host(config['host'], config['port'])

        if alive:
            config['latency'] = latency
            working.append(config)
            print(f"✅ {latency:.0f}ms")
        else:
            print("❌")

    # Сортируем по задержке
    working.sort(key=lambda x: x['latency'])

    print(f"\n📊 Working configs: {len(working)} / {min(max_to_test, len(configs))}")
    if working:
        print(f"🏆 Best latency: {working[0]['latency']:.0f}ms")

    return working


def save_subscription(configs: list, max_count: int = 25) -> Path:
    """
    Сохраняет топ-N конфигураций в файл подписки
    """
    top_configs = configs[:max_count]

    # Создаём папку если её нет
    Path("subscriptions").mkdir(exist_ok=True)

    filepath = Path("subscriptions") / "whitelist_top15.txt"

    with open(filepath, 'w', encoding='utf-8') as f:
        # Заголовок подписки
        f.write("# profile-title: 🏳️ WhiteList VPN - Top 25\n")
        f.write("# profile-update-interval: 1\n")
        f.write(f"# Date/Time: {datetime.now().strftime('%Y-%m-%d / %H:%M')}\n")
        f.write(f"# Total tested: {len(configs)}\n")
        f.write(f"# Working: {len(top_configs)}\n")
        if top_configs:
            f.write(f"# Best latency: {top_configs[0]['latency']:.0f}ms\n")
        f.write("# Auto-updated every hour\n")
        f.write("\n")

        # Конфигурации
        for i, config in enumerate(top_configs, 1):
            f.write(f"# {i}. {config['name']} - {config['latency']:.0f}ms\n")
            f.write(f"{config['raw']}\n")

    print(f"\n✅ Subscription saved: {filepath}")
    return filepath


def push_to_github() -> bool:
    """
    Отправляет изменения на GitHub с предварительным pull
    """
    print("\n📤 PUSHING TO GITHUB")
    print("-" * 40)

    # Настраиваем Git
    subprocess.run(['git', 'config', '--local', 'user.email', 'github-actions[bot]@users.noreply.github.com'],
                   capture_output=True)
    subprocess.run(['git', 'config', '--local', 'user.name', 'github-actions[bot]'],
                   capture_output=True)

    # Добавляем файл
    result = subprocess.run(['git', 'add', 'subscriptions/whitelist_top15.txt'],
                            capture_output=True)
    if result.returncode != 0:
        print("❌ Failed to add file to git")
        return False

    # Проверяем, есть ли изменения
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if result.returncode == 0:
        print("ℹ️ No changes to commit")
        return True

    # Коммитим
    commit_msg = f'Auto-update {datetime.now().strftime("%Y-%m-%d %H:%M")}'
    result = subprocess.run(['git', 'commit', '-m', commit_msg], capture_output=True)
    if result.returncode != 0:
        print(f"❌ Failed to commit: {result.stderr.decode() if result.stderr else 'unknown error'}")
        return False
    print(f"✅ Committed: {commit_msg}")

    # Стягиваем последние изменения с GitHub (важно!)
    print("🔄 Pulling latest changes...")
    result = subprocess.run(['git', 'pull', '--rebase'], capture_output=True)
    if result.returncode != 0:
        print(f"⚠️ Warning: git pull had issues: {result.stderr.decode() if result.stderr else 'unknown'}")
        # Продолжаем, возможно конфликтов нет

    # Пушим
    print("📤 Pushing to GitHub...")
    result = subprocess.run(['git', 'push'], capture_output=True)
    if result.returncode != 0:
        print(f"❌ Failed to push: {result.stderr.decode() if result.stderr else 'unknown error'}")
        return False

    print("✅ Pushed to GitHub successfully!")
    return True


def main():
    """Основная функция"""
    start_time = time.time()

    # Шаг 1: Сбор конфигураций
    all_configs = collect_configs()

    if not all_configs:
        print("\n❌ No configurations found!")
        return 1

    # Шаг 2: Тестирование задержки
    working_configs = test_configs(all_configs, max_to_test=500)

    if not working_configs:
        print("\n❌ No working configurations found!")
        return 1

    # Шаг 3: Сохранение подписки
    save_subscription(working_configs, max_count=25)

    # Шаг 4: Отправка на GitHub
    push_to_github()

    # Вывод итогов
    elapsed = time.time() - start_time

    print("\n" + "=" * 60)
    print("📊 FINAL SUMMARY")
    print("=" * 60)
    print(f"⏱️  Total time: {elapsed:.1f} seconds")
    print(f"📥 Configs collected: {len(all_configs)}")
    print(f"✅ Working configs: {len(working_configs)}")
    print(f"🏆 Top 15 saved to: subscriptions/whitelist_top15.txt")
    print("\n🔗 Subscription URL:")
    print("   https://raw.githubusercontent.com/dmeshechkov/vpn-subscriptions/main/subscriptions/whitelist_top15.txt")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
