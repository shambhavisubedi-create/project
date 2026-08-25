import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv
import os

load_dotenv()

try:
    # 1. Establish the connection to the MySQL server
    connection = mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "gogather"),
    )

    if connection.is_connected():
        print("Successfully connected to the database!")
        
        # 2. Create a cursor object to execute SQL commands
        cursor = connection.cursor()
        
        # 3. Execute a sample query
        cursor.execute("SELECT DATABASE();")
        record = cursor.fetchone()
        print(f"You are connected to database: {record[0]}")

except Error as e:
    print(f"Error while connecting to MySQL: {e}")

finally:
    # 4. Always close the cursor and connection when done
    if 'connection' in locals() and connection.is_connected():
        cursor.close()
        connection.close()
        print("MySQL connection is closed.")
