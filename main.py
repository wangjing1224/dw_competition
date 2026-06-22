"""保留 uv 默认命令入口，并提示正式后端启动方式。"""


def main():
    print("请使用: uv run uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
