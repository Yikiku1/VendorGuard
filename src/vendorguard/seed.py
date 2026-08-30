import asyncio

from vendorguard.config import Settings, load_settings
from vendorguard.database import (
    create_database_engine,
    create_session_factory,
    transactional_session,
)
from vendorguard.security import seed_demo_users


async def seed_database(settings: Settings) -> int:
    """在单个事务中写入演示账号并返回新增数量。"""

    engine = create_database_engine(settings)

    try:
        session_factory = create_session_factory(engine)

        async with transactional_session(session_factory) as session:
            created_users = await seed_demo_users(session, settings)

        return len(created_users)
    finally:
        await engine.dispose()


def main() -> None:
    """运行数据库种子命令并输出创建结果。"""

    created_count = asyncio.run(seed_database(load_settings()))
    print(f"种子创建完成: 已创建 {created_count} 个演示帐户。")


if __name__ == "__main__":
    main()
