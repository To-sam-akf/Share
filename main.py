from __future__ import annotations

import time
from pathlib import Path

from config import load_config
from discovery import DiscoveryService, make_device_id
from discovery.udp_discovery import get_local_ip
from transfer import TCPFileServer, send_file


def format_device_line(index: int, device) -> str:
    age = max(0, int(time.time() - device.last_seen))
    return (
        f"{index}. {device.device_name} "
        f"({device.ip}:{device.tcp_port}, {device.status}, {age}s ago)"
    )


def list_devices(discovery: DiscoveryService) -> list:
    devices = discovery.list_devices()
    if not devices:
        print("暂未发现其他设备。请确认对方程序已启动，且防火墙允许 UDP 广播。")
        return []

    print("\n在线设备：")
    for index, device in enumerate(devices, start=1):
        print(format_device_line(index, device))
    return devices


def choose_device(discovery: DiscoveryService):
    devices = list_devices(discovery)
    if not devices:
        return None

    choice = input("请选择目标设备编号：").strip()
    try:
        selected_index = int(choice)
    except ValueError:
        print("请输入有效的数字编号。")
        return None

    if selected_index < 1 or selected_index > len(devices):
        print("设备编号超出范围。")
        return None
    return devices[selected_index - 1]


def handle_send_file(discovery: DiscoveryService, chunk_size: int) -> None:
    device = choose_device(discovery)
    if device is None:
        return

    raw_path = input("请输入要发送的文件路径：").strip()
    file_path = Path(raw_path).expanduser()
    if not file_path.is_file():
        print(f"文件不存在：{file_path}")
        return

    try:
        result = send_file(device.ip, device.tcp_port, file_path, chunk_size=chunk_size)
    except OSError as exc:
        print(f"发送失败：{exc}")
        return

    if result.get("status") == "success":
        print(
            "发送成功："
            f"{result.get('file_name')} "
            f"({result.get('bytes_received')} bytes)"
        )
    else:
        print(f"发送失败：{result.get('message', '接收端返回未知错误')}")


def print_menu() -> None:
    print("\n请选择操作：")
    print("1. 查看在线设备")
    print("2. 发送单个文件")
    print("0. 退出")


def main() -> int:
    config = load_config()
    device_id = make_device_id(config.device_name, config.tcp_port)

    tcp_server = TCPFileServer(
        host="0.0.0.0",
        port=config.tcp_port,
        shared_folder=config.shared_folder,
        chunk_size=config.chunk_size,
    )
    discovery = DiscoveryService(
        device_id=device_id,
        device_name=config.device_name,
        udp_port=config.udp_port,
        tcp_port=config.tcp_port,
        broadcast_ip=config.broadcast_ip,
    )

    tcp_server.start()
    discovery.start()

    print("局域网文件共享系统已启动")
    print(f"设备名称：{config.device_name}")
    print(f"本机 IP：{get_local_ip()}")
    print(f"UDP 发现端口：{config.udp_port}")
    print(f"TCP 接收端口：{tcp_server.bound_port}")
    print(f"接收目录：{config.shared_folder}")

    try:
        while True:
            print_menu()
            choice = input("> ").strip()
            if choice == "1":
                list_devices(discovery)
            elif choice == "2":
                handle_send_file(discovery, config.chunk_size)
            elif choice == "0":
                break
            else:
                print("未知操作，请重新选择。")
    except KeyboardInterrupt:
        print("\n正在退出...")
    finally:
        discovery.stop()
        tcp_server.stop()
        print("已停止。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
