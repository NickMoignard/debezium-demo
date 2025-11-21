#!/usr/bin/env python3
"""
Fake data generator for Debezium CDC demo.
Inserts 1-2 records per second into products and sales tables.
"""
import os
import time
import random
from faker import Faker
import psycopg2
from psycopg2.extras import execute_values

from logger import get_logger

# Initialize logger
logger = get_logger()

# Database connection settings
DB_HOST = os.getenv("DB_HOST", "postgres-source")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "sourcedb")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

fake = Faker()

logger.info("🔧 Configuration:")
logger.info(f"   DB_HOST: {DB_HOST}")
logger.info(f"   DB_PORT: {DB_PORT}")
logger.info(f"   DB_NAME: {DB_NAME}")
logger.info(f"   DB_USER: {DB_USER}")


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
    """Create products and sales tables if they don't exist."""
    logger.info("📋 Creating/verifying tables...")
    
    with conn.cursor() as cur:
        # Create products table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                category VARCHAR(100),
                price NUMERIC(10, 2),
                stock_quantity INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("   ✓ products table ready")
        
        # Create sales table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id SERIAL PRIMARY KEY,
                product_id INTEGER,
                customer_name VARCHAR(255),
                customer_email VARCHAR(255),
                quantity INTEGER,
                total_amount NUMERIC(10, 2),
                sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("   ✓ sales table ready")
        
        # Set replica identity to FULL for CDC with before/after values
        cur.execute("ALTER TABLE products REPLICA IDENTITY FULL")
        logger.info("   ✓ products replica identity set to FULL")
        
        cur.execute("ALTER TABLE sales REPLICA IDENTITY FULL")
        logger.info("   ✓ sales replica identity set to FULL")
        
        conn.commit()
        logger.info("✅ Tables created/verified successfully")


def generate_product():
    """Generate fake product data."""
    categories = ["Electronics", "Clothing", "Food", "Books", "Home", "Sports", "Toys"]
    return {
        "name": fake.catch_phrase(),
        "category": random.choice(categories),
        "price": round(random.uniform(5.99, 999.99), 2),
        "stock_quantity": random.randint(0, 500)
    }


def generate_sale(product_ids):
    """Generate fake sale data."""
    quantity = random.randint(1, 10)
    price = round(random.uniform(5.99, 999.99), 2)
    return {
        "product_id": random.choice(product_ids) if product_ids else None,
        "customer_name": fake.name(),
        "customer_email": fake.email(),
        "quantity": quantity,
        "total_amount": round(price * quantity, 2)
    }


def insert_products(conn, count=2):
    """Insert fake products into the database."""
    products = [generate_product() for _ in range(count)]
    
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO products (name, category, price, stock_quantity)
            VALUES %s
            RETURNING id
            """,
            [(p["name"], p["category"], p["price"], p["stock_quantity"]) for p in products]
        )
        inserted_ids = [row[0] for row in cur.fetchall()]
        conn.commit()
    
    logger.debug(f"📦 Inserted {count} products (IDs: {inserted_ids})")
    return inserted_ids


def insert_sales(conn, product_ids, count=2):
    """Insert fake sales into the database."""
    sales = [generate_sale(product_ids) for _ in range(count)]
    
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO sales (product_id, customer_name, customer_email, quantity, total_amount)
            VALUES %s
            RETURNING id
            """,
            [(s["product_id"], s["customer_name"], s["customer_email"], 
              s["quantity"], s["total_amount"]) for s in sales]
        )
        inserted_ids = [row[0] for row in cur.fetchall()]
        conn.commit()
    
    logger.debug(f"💰 Inserted {count} sales (IDs: {inserted_ids})")


def get_random_product_ids(conn, limit=100):
    """Get random product IDs for generating sales."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY RANDOM() LIMIT %s", (limit,))
        return [row[0] for row in cur.fetchall()]


def main():
    logger.info("🚀 Starting Debezium data generator...")
    
    # Connect to database
    conn = get_db_connection()
    
    # Create tables
    create_tables(conn)
    
    # Seed some initial products
    logger.info("🌱 Seeding initial products...")
    product_ids = insert_products(conn, count=20)
    logger.info(f"✅ Seeded {len(product_ids)} initial products")
    
    logger.info("📊 Starting continuous data generation (1-2 records/sec per table)...")
    logger.info("💡 Press Ctrl+C to stop")
    
    iteration = 0
    total_products = len(product_ids)
    total_sales = 0
    
    try:
        while True:
            iteration += 1
            
            # Insert 1-2 products
            product_count = random.randint(1, 2)
            new_product_ids = insert_products(conn, count=product_count)
            product_ids.extend(new_product_ids)
            total_products += product_count
            
            # Keep a reasonable pool of product IDs
            if len(product_ids) > 100:
                product_ids = get_random_product_ids(conn, limit=100)
            
            # Insert 1-2 sales
            sale_count = random.randint(1, 2)
            insert_sales(conn, product_ids, count=sale_count)
            total_sales += sale_count
            
            if iteration % 10 == 0:
                logger.info(f"📈 Progress: {total_products} products, {total_sales} sales (iteration {iteration})")
            
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
        logger.info(f"📊 Final stats: {total_products} products, {total_sales} sales")


if __name__ == "__main__":
    main()
