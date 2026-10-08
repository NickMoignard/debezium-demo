#!/usr/bin/env python3
"""Existing regional table schemas and natural payload factories."""
import os
import time
import random
from faker import Faker
import psycopg2

from logger import get_logger

# Faker locales per region, so names, addresses and phone numbers look local
REGION_LOCALES = {
    "au": ["en_AU"],
    "us": ["en_US"],
    "uk": ["en_GB"],
}
REGIONS = list(REGION_LOCALES)

CATEGORIES = ["Electronics", "Clothing", "Food", "Books", "Home", "Sports", "Toys"]
PROMO_CODES = ["WELCOME10", "SPRING25", "FREESHIP", "VIP15", "FLASH50"]

# Initialize logger
logger = get_logger()

# Database connection settings
DB_HOST = os.getenv("DB_HOST", "postgres-source")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "sourcedb")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

# Share of generated rows that deliberately break a DQ expectation downstream
BAD_DATA_RATE = 0.02

fakers = {
    locale: Faker(locale)
    for locales in REGION_LOCALES.values()
    for locale in locales
}



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
                password=DB_PASSWORD,
                connect_timeout=10,
                application_name="cdc-generator",
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

        logger.info("Tables created or verified")


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



if __name__ == "__main__":
    from workload import main
    main()
