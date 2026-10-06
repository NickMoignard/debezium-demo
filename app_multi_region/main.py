#!/usr/bin/env python3
"""
Fake data generator for the multi-region Debezium CDC demo.

Each region gets its own schema (standing in for a separate regional database)
with products, users, orders and line_items tables. After seeding products and
users, the generator keeps placing orders with line items in every region and
moves existing orders through their status lifecycle so CDC sees updates too.
"""
import os
import time
import random
from faker import Faker
import psycopg2
from psycopg2.extras import execute_values

from logger import get_logger

# Faker locales per region, so names, addresses and phone numbers look local
REGION_LOCALES = {
    "au": ["en_AU"],
    "us": ["en_US"],
    "eu": ["de_DE", "fr_FR", "es_ES", "it_IT", "nl_NL"],
    "uk": ["en_GB"],
    "ca": ["en_CA"],
}
REGIONS = list(REGION_LOCALES)

SEED_PRODUCTS_PER_REGION = 20
SEED_USERS_PER_REGION = 50

CATEGORIES = ["Electronics", "Clothing", "Food", "Books", "Home", "Sports", "Toys"]
PROMO_CODES = ["WELCOME10", "SPRING25", "FREESHIP", "VIP15", "FLASH50"]

# Next status for each non-terminal order status
ORDER_STATUS_FLOW = {
    "pending": "paid",
    "paid": "shipped",
    "shipped": "delivered",
}

# Initialize logger
logger = get_logger()

# Database connection settings
DB_HOST = os.getenv("DB_HOST", "postgres-source")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "sourcedb")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

# Share of generated rows that deliberately break a DQ expectation downstream
BAD_DATA_RATE = float(os.getenv("BAD_DATA_RATE", "0.02"))
# Chance per region per iteration of deleting one cancelled order
DELETE_RATE = float(os.getenv("DELETE_RATE", "0.05"))

fakers = {
    locale: Faker(locale)
    for locales in REGION_LOCALES.values()
    for locale in locales
}

logger.info("🔧 Configuration:")
logger.info(f"   DB_HOST: {DB_HOST}")
logger.info(f"   DB_PORT: {DB_PORT}")
logger.info(f"   DB_NAME: {DB_NAME}")
logger.info(f"   DB_USER: {DB_USER}")
logger.info(f"   REGIONS: {', '.join(REGIONS)}")
logger.info(f"   BAD_DATA_RATE: {BAD_DATA_RATE}")
logger.info(f"   DELETE_RATE: {DELETE_RATE}")


def is_bad():
    """Roll for whether this row should carry a deliberate data-quality defect."""
    return random.random() < BAD_DATA_RATE


def get_db_connection():
    """Create database connection with retry logic."""
    max_retries = 5
    retry_delay = 2

    logger.info("🔌 Attempting to connect to PostgreSQL...")

    for attempt in range(max_retries):
        try:
            logger.info(f"   Attempt {attempt + 1}/{max_retries}...")
            conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD
            )
            logger.info(f"✅ Connected to PostgreSQL at {DB_HOST}:{DB_PORT}/{DB_NAME}")
            return conn
        except psycopg2.OperationalError as e:
            logger.error(f"❌ Connection failed: {e}")
            if attempt < max_retries - 1:
                logger.info(f"⏳ Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                logger.critical(f"💥 Failed to connect after {max_retries} attempts")
                raise Exception(f"Failed to connect after {max_retries} attempts: {e}")


def create_tables(conn):
    """Create & configure tables if they don't exist."""
    logger.info("📋 Creating/verifying tables...")

    with conn.cursor() as cur:
        for region in REGIONS:

            cur.execute(f"""
                CREATE SCHEMA IF NOT EXISTS {region}
            """)

            # Create products table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {region}.products (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    category VARCHAR(100),
                    price NUMERIC(10, 2),
                    stock_quantity INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info(f"   ✓ {region}.products table ready")

            # Create users table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {region}.users (
                    id SERIAL PRIMARY KEY,
                    first_name VARCHAR(255) NOT NULL,
                    last_name VARCHAR(255) NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    phone_number VARCHAR(20),
                    address_line_one VARCHAR(255),
                    address_line_two VARCHAR(255),
                    city VARCHAR(100),
                    state VARCHAR(100),
                    postal_code VARCHAR(20),
                    country VARCHAR(100),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info(f"   ✓ {region}.users table ready")

            # Create orders table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {region}.orders (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER,
                    order_status VARCHAR(50),
                    promo_code VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info(f"   ✓ {region}.orders table ready")

            # Create line_items table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {region}.line_items (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER,
                    order_id INTEGER,
                    quantity INTEGER,
                    line_item_discount NUMERIC(10, 2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            logger.info(f"   ✓ {region}.line_items table ready")

            # Set replica identity to FULL for CDC with before/after values
            for table in ["products", "users", "orders", "line_items"]:
                cur.execute(f"ALTER TABLE {region}.{table} REPLICA IDENTITY FULL")
                logger.info(f"   ✓ {region}.{table} replica identity set to FULL")

        conn.commit()
        logger.info("✅ Tables created/verified successfully")


def generate_product(region):
    """Generate fake product data."""
    fake = fakers[REGION_LOCALES[region][0]]
    return {
        "name": fake.catch_phrase(),
        "category": random.choice(CATEGORIES),
        # Bad row: a product with no price (fails price_present)
        "price": None if is_bad() else round(random.uniform(5.99, 999.99), 2),
        "stock_quantity": random.randint(0, 500)
    }


def generate_user(region):
    """Generate fake user data, localised to one of the region's locales."""
    fake = fakers[random.choice(REGION_LOCALES[region])]
    # Not every locale has a secondary address provider
    try:
        address_line_two = fake.secondary_address() if random.random() < 0.3 else None
    except AttributeError:
        address_line_two = None

    return {
        "first_name": fake.first_name(),
        "last_name": fake.last_name(),
        "email": fake.email(),
        "phone_number": fake.phone_number()[:20],
        "address_line_one": fake.street_address(),
        "address_line_two": address_line_two,
        "city": fake.city(),
        "state": fake.administrative_unit(),
        "postal_code": fake.postcode(),
        "country": fake.current_country(),
    }


def generate_order(user_ids):
    """Generate fake order data."""
    order = {
        "user_id": random.choice(user_ids),
        "order_status": "pending",
        "promo_code": random.choice(PROMO_CODES) if random.random() < 0.2 else None,
    }
    # Bad rows: an orphan order (has_customer) or a status nobody knows (known_status)
    if is_bad():
        if random.random() < 0.5:
            order["user_id"] = None
        else:
            order["order_status"] = "lost"
    return order


def generate_line_item(order_id, product_ids):
    """Generate fake line item data for an order."""
    discount = round(random.uniform(1, 50), 2) if random.random() < 0.15 else 0
    line_item = {
        "product_id": random.choice(product_ids),
        "order_id": order_id,
        "quantity": random.randint(1, 5),
        "line_item_discount": discount,
    }
    # Bad rows: zero quantity (positive_quantity) or a negative discount (discount_non_negative)
    if is_bad():
        if random.random() < 0.5:
            line_item["quantity"] = 0
        else:
            line_item["line_item_discount"] = -round(random.uniform(1, 50), 2)
    return line_item


def insert_products(conn, region, count=2):
    """Insert fake products into a region's schema."""
    products = [generate_product(region) for _ in range(count)]

    with conn.cursor() as cur:
        inserted = execute_values(
            cur,
            f"""
            INSERT INTO {region}.products (name, category, price, stock_quantity)
            VALUES %s
            RETURNING id
            """,
            [(p["name"], p["category"], p["price"], p["stock_quantity"]) for p in products],
            fetch=True
        )
        inserted_ids = [row[0] for row in inserted]
        conn.commit()

    logger.debug(f"📦 [{region}] Inserted {count} products (IDs: {inserted_ids})")
    return inserted_ids


def insert_users(conn, region, count=2):
    """Insert fake users into a region's schema."""
    users = [generate_user(region) for _ in range(count)]

    with conn.cursor() as cur:
        inserted = execute_values(
            cur,
            f"""
            INSERT INTO {region}.users (
                first_name, last_name, email, phone_number,
                address_line_one, address_line_two, city, state, postal_code, country
            )
            VALUES %s
            RETURNING id
            """,
            [(u["first_name"], u["last_name"], u["email"], u["phone_number"],
              u["address_line_one"], u["address_line_two"], u["city"], u["state"],
              u["postal_code"], u["country"]) for u in users],
            fetch=True
        )
        inserted_ids = [row[0] for row in inserted]
        conn.commit()

    logger.debug(f"👤 [{region}] Inserted {count} users (IDs: {inserted_ids})")
    return inserted_ids


def insert_orders(conn, region, user_ids, product_ids, count=2):
    """Insert fake orders, each with 1-4 line items, in a single transaction."""
    orders = [generate_order(user_ids) for _ in range(count)]

    with conn.cursor() as cur:
        inserted = execute_values(
            cur,
            f"""
            INSERT INTO {region}.orders (user_id, order_status, promo_code)
            VALUES %s
            RETURNING id
            """,
            [(o["user_id"], o["order_status"], o["promo_code"]) for o in orders],
            fetch=True
        )
        order_ids = [row[0] for row in inserted]

        line_items = [
            generate_line_item(order_id, product_ids)
            for order_id in order_ids
            for _ in range(random.randint(1, 4))
        ]
        execute_values(
            cur,
            f"""
            INSERT INTO {region}.line_items (product_id, order_id, quantity, line_item_discount)
            VALUES %s
            """,
            [(li["product_id"], li["order_id"], li["quantity"], li["line_item_discount"])
             for li in line_items]
        )
        conn.commit()

    logger.debug(f"🛒 [{region}] Inserted {count} orders (IDs: {order_ids}) with {len(line_items)} line items")
    return len(order_ids), len(line_items)


def advance_order_statuses(conn, region, count=1):
    """Move a few open orders to their next status (or cancel them) to produce CDC updates."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, order_status FROM {region}.orders
            WHERE order_status = ANY(%s)
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (list(ORDER_STATUS_FLOW), count)
        )
        open_orders = cur.fetchall()
        for order_id, status in open_orders:
            # Occasionally cancel an order before it ships
            if status != "shipped" and random.random() < 0.05:
                new_status = "cancelled"
            else:
                new_status = ORDER_STATUS_FLOW[status]
            cur.execute(
                f"""
                UPDATE {region}.orders
                SET order_status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_status, order_id)
            )
        conn.commit()

    logger.debug(f"🚚 [{region}] Advanced {len(open_orders)} order statuses")


def delete_cancelled_order(conn, region):
    """Delete one cancelled order and its line items, so CDC carries deletes too."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {region}.orders
            WHERE id = (SELECT id FROM {region}.orders WHERE order_status = 'cancelled' LIMIT 1)
            RETURNING id
            """
        )
        deleted = cur.fetchone()
        if deleted:
            cur.execute(f"DELETE FROM {region}.line_items WHERE order_id = %s", (deleted[0],))
        conn.commit()

    if deleted:
        logger.debug(f"🗑 [{region}] Deleted cancelled order {deleted[0]}")


def get_random_ids(conn, region, table, limit=100):
    """Get random IDs from a region's table for generating related rows."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {region}.{table} ORDER BY RANDOM() LIMIT %s", (limit,))
        return [row[0] for row in cur.fetchall()]


def main():
    logger.info("🚀 Starting multi-region Debezium data generator...")

    # Connect to database
    conn = get_db_connection()

    # Create tables
    create_tables(conn)

    # Seed products and users per region
    logger.info("🌱 Seeding initial products and users...")
    product_ids = {}
    user_ids = {}
    for region in REGIONS:
        product_ids[region] = insert_products(conn, region, count=SEED_PRODUCTS_PER_REGION)
        user_ids[region] = insert_users(conn, region, count=SEED_USERS_PER_REGION)
        logger.info(f"   ✓ [{region}] Seeded {len(product_ids[region])} products, {len(user_ids[region])} users")
    logger.info("✅ Seeding complete")

    logger.info("📊 Starting continuous order generation (1-2 orders/sec per region)...")
    logger.info("💡 Press Ctrl+C to stop")

    iteration = 0
    totals = {
        "products": SEED_PRODUCTS_PER_REGION * len(REGIONS),
        "users": SEED_USERS_PER_REGION * len(REGIONS),
        "orders": 0,
        "line_items": 0,
    }

    try:
        while True:
            iteration += 1

            for region in REGIONS:
                # New products and users trickle in more slowly than orders
                if random.random() < 0.1:
                    product_ids[region].extend(insert_products(conn, region, count=1))
                    totals["products"] += 1
                if random.random() < 0.3:
                    user_ids[region].extend(insert_users(conn, region, count=1))
                    totals["users"] += 1

                # Keep a reasonable pool of IDs to pick from
                if len(product_ids[region]) > 100:
                    product_ids[region] = get_random_ids(conn, region, "products", limit=100)
                if len(user_ids[region]) > 200:
                    user_ids[region] = get_random_ids(conn, region, "users", limit=200)

                # Insert 1-2 orders with line items
                order_count, line_item_count = insert_orders(
                    conn, region, user_ids[region], product_ids[region],
                    count=random.randint(1, 2)
                )
                totals["orders"] += order_count
                totals["line_items"] += line_item_count

                # Progress some existing orders through their lifecycle
                advance_order_statuses(conn, region, count=random.randint(0, 2))

                if random.random() < DELETE_RATE:
                    delete_cancelled_order(conn, region)

            if iteration % 10 == 0:
                logger.info(
                    f"📈 Progress: {totals['products']} products, {totals['users']} users, "
                    f"{totals['orders']} orders, {totals['line_items']} line items "
                    f"across {len(REGIONS)} regions (iteration {iteration})"
                )

            # Sleep 1 second between batches
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("⏹ Stopping data generator...")
    except Exception as e:
        logger.exception(f"💥 Error occurred: {e}")
        raise
    finally:
        conn.close()
        logger.info("✅ Database connection closed")
        logger.info(
            f"📊 Final stats: {totals['products']} products, {totals['users']} users, "
            f"{totals['orders']} orders, {totals['line_items']} line items"
        )


if __name__ == "__main__":
    main()
