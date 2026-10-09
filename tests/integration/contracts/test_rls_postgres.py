"""ST-03: effective row security under an actual nonowner/NOBYPASSRLS role."""

from uuid import uuid4

import asyncpg
import pytest

from app.database import set_user_context
from app.db.rls import setup_row_level_security

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def tenant_role(db_conn):
    role = "contract_tenant_" + uuid4().hex
    await db_conn.execute(f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOLOGIN')
    try:
        await db_conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await db_conn.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON public.long_term_memory,public.memory_nodes,"
            "public.memory_node_sources,public.private_data_leases,public.group_chats,"
            f'public.group_members,public.group_messages TO "{role}"'
        )
        await db_conn.execute(
            f'GRANT USAGE ON SEQUENCE public.long_term_memory_id_seq,public.group_messages_id_seq TO "{role}"'
        )
        yield role
    finally:
        await db_conn.execute(f'DROP OWNED BY "{role}"')
        await db_conn.execute(f'DROP ROLE "{role}"')


async def configure(connection):
    async def query(sql, params=()):
        return await connection.fetch(sql, *params)

    await setup_row_level_security(query)


@pytest.mark.parametrize("table", ["long_term_memory", "memory_node_sources", "private_data_leases"])
async def test_nonowner_role_denies_foreign_crud_and_allows_own(contract_pool, db_conn, tenant_role, table):
    # Owner-filtered repository tests cannot detect absent USING/WITH CHECK policies.
    await configure(db_conn)
    await db_conn.execute("INSERT INTO users(user_id) VALUES(84001),(84002)")
    await db_conn.execute("INSERT INTO chats(user_id) VALUES(84001),(84002)")
    own = await db_conn.fetchval(
        "INSERT INTO long_term_memory(user_id,content) VALUES(84001,'Own source') RETURNING id"
    )
    foreign = await db_conn.fetchval(
        "INSERT INTO long_term_memory(user_id,content) VALUES(84002,'Foreign source') RETURNING id"
    )
    own_node = await db_conn.fetchval(
        "INSERT INTO memory_nodes(user_id,entity_name,entity_type) VALUES(84001,'own','concept') RETURNING id"
    )
    foreign_node = await db_conn.fetchval(
        "INSERT INTO memory_nodes(user_id,entity_name,entity_type) VALUES(84002,'foreign','concept') RETURNING id"
    )
    await db_conn.execute(
        "INSERT INTO memory_node_sources(node_id,memory_id,user_id,entity_type) VALUES($1,$2,84001,'concept'),($3,$4,84002,'concept')",
        own_node,
        own,
        foreign_node,
        foreign,
    )
    own_extra = await db_conn.fetchval(
        "INSERT INTO long_term_memory(user_id,content) VALUES(84001,'Extra source') RETURNING id"
    )
    await db_conn.execute(
        "INSERT INTO private_data_leases(lease_id,user_id,memory_epoch,purpose,expires_at) VALUES($1,84001,1,'contract',now()+interval '1 minute'),($2,84002,2,'contract',now()+interval '1 minute')",
        uuid4(),
        uuid4(),
    )
    own_insert = {
        "long_term_memory": (
            "INSERT INTO long_term_memory(user_id,content) VALUES(84001,'Inserted own') RETURNING user_id",
            (),
        ),
        "memory_node_sources": (
            "INSERT INTO memory_node_sources(node_id,memory_id,user_id,entity_type) VALUES($1,$2,84001,'concept') RETURNING user_id",
            (own_node, own_extra),
        ),
        "private_data_leases": (
            "INSERT INTO private_data_leases(lease_id,user_id,memory_epoch,purpose,expires_at) VALUES($1,84001,1,'own',now()+interval '1 minute') RETURNING user_id",
            (uuid4(),),
        ),
    }
    foreign_insert = {
        "long_term_memory": ("INSERT INTO long_term_memory(user_id,content) VALUES(84002,'Forbidden')", ()),
        "memory_node_sources": (
            "INSERT INTO memory_node_sources(node_id,memory_id,user_id,entity_type) VALUES($1,$2,84002,'concept')",
            (foreign_node, foreign),
        ),
        "private_data_leases": (
            "INSERT INTO private_data_leases(lease_id,user_id,memory_epoch,purpose,expires_at) VALUES($1,84002,2,'forbidden',now()+interval '1 minute')",
            (uuid4(),),
        ),
    }
    async with contract_pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(f'SET LOCAL ROLE "{tenant_role}"')
            properties = await connection.fetchrow(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
            assert tuple(properties) == (False, False)
            assert await connection.fetchval(
                "SELECT pg_get_userbyid(relowner) <> current_user FROM pg_class WHERE oid=$1::regclass", table
            )
            await set_user_context(84001, False, conn=connection)
            assert set(await connection.fetchval(f"SELECT array_agg(DISTINCT user_id) FROM {table}")) == {84001}
            assert await connection.execute(f"UPDATE {table} SET user_id=84002 WHERE user_id=84002") == "UPDATE 0"
            assert await connection.execute(f"DELETE FROM {table} WHERE user_id=84002") == "DELETE 0"
            sql, params = own_insert[table]
            assert await connection.fetchval(sql, *params) == 84001
            sql, params = foreign_insert[table]
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
                async with connection.transaction():
                    await connection.execute(sql, *params)
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
                async with connection.transaction():
                    await connection.execute(f"UPDATE {table} SET user_id=84002 WHERE user_id=84001")
            assert await connection.execute(f"DELETE FROM {table} WHERE user_id=84001") != "DELETE 0"
        async with connection.transaction():
            await connection.execute(f'SET LOCAL ROLE "{tenant_role}"')
            assert await connection.fetchval("SELECT current_setting('app.user_id',true)") in {None, ""}
            assert await connection.fetchval("SELECT current_setting('app.is_admin',true)") in {None, "", "false"}
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 0
            await set_user_context(0, True, conn=connection)
            assert await connection.fetchval(f"SELECT count(*) FROM {table} WHERE user_id=84002") == 1
    async with contract_pool.acquire() as reacquired:
        assert await reacquired.fetchval("SELECT current_setting('app.user_id',true)") in {None, ""}
        assert await reacquired.fetchval("SELECT current_setting('app.is_admin',true)") in {None, "", "false"}


async def test_group_membership_controls_message_visibility(contract_pool, db_conn, tenant_role):
    before = await db_conn.fetchrow(
        "SELECT oid,pg_get_expr(polqual,polrelid) AS qual,pg_get_expr(polwithcheck,polrelid) AS check FROM pg_policy WHERE polname='group_members_policy' AND polrelid='group_members'::regclass"
    )
    assert "group_members" in before["qual"]
    await configure(db_conn)
    repaired = await db_conn.fetchrow(
        "SELECT oid,pg_get_expr(polqual,polrelid) AS qual,pg_get_expr(polwithcheck,polrelid) AS check FROM pg_policy WHERE polname='group_members_policy' AND polrelid='group_members'::regclass"
    )
    assert repaired["oid"] == before["oid"]
    await configure(db_conn)
    assert (
        await db_conn.fetchrow(
            "SELECT oid,pg_get_expr(polqual,polrelid) AS qual,pg_get_expr(polwithcheck,polrelid) AS check FROM pg_policy WHERE polname='group_members_policy' AND polrelid='group_members'::regclass"
        )
        == repaired
    )
    await db_conn.execute("INSERT INTO users(user_id) VALUES(84001),(84002)")
    await db_conn.execute(
        "INSERT INTO group_chats(chat_id,title,admin_user_id) VALUES(-84001,'Own group',84001),(-84002,'Foreign group',84002)"
    )
    await db_conn.execute("INSERT INTO group_members(chat_id,user_id) VALUES(-84001,84001),(-84002,84002)")
    await db_conn.execute(
        "INSERT INTO group_messages(chat_id,user_id,message_text) VALUES(-84001,84001,'Own group text'),(-84002,84002,'Foreign group text')"
    )
    async with contract_pool.acquire() as connection, connection.transaction():
        await connection.execute(f'SET LOCAL ROLE "{tenant_role}"')
        await set_user_context(84001, False, conn=connection)
        assert await connection.fetchval("SELECT array_agg(user_id) FROM group_members") == [84001]
        assert await connection.fetchval("SELECT array_agg(chat_id) FROM group_messages") == [-84001]
        assert await connection.execute("DELETE FROM group_messages WHERE chat_id=-84002") == "DELETE 0"
        assert (
            await connection.execute("UPDATE group_messages SET message_text='Own edit' WHERE chat_id=-84001")
            == "UPDATE 1"
        )
        for sql in (
            "INSERT INTO group_members(chat_id,user_id) VALUES(-84002,84001)",
            "UPDATE group_members SET chat_id=-84002 WHERE user_id=84001",
            "INSERT INTO group_messages(chat_id,user_id,message_text) VALUES(-84002,84001,'Forbidden foreign group')",
            "UPDATE group_messages SET chat_id=-84002 WHERE chat_id=-84001",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
                async with connection.transaction():
                    await connection.execute(sql)
        assert (
            await connection.execute(
                "INSERT INTO group_messages(chat_id,user_id,message_text) VALUES(-84001,84001,'Allowed own group')"
            )
            == "INSERT 0 1"
        )
        assert await connection.execute("DELETE FROM group_members WHERE user_id=84001") == "DELETE 1"
        assert await connection.fetchval("SELECT count(*) FROM group_messages") == 0
        await set_user_context(0, True, conn=connection)
        assert await connection.fetchval("SELECT count(*) FROM group_messages") == 3
        assert (
            await connection.execute("INSERT INTO group_members(chat_id,user_id) VALUES(-84002,84001)") == "INSERT 0 1"
        )
        assert await connection.fetchval("SELECT count(*) FROM group_members") == 2
