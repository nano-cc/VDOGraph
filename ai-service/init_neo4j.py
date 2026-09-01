#!/usr/bin/env python3
"""
初始化 Neo4j 索引
"""
from app.clients.neo4j_client import Neo4jClient
from app.core.logging import logger

def main():
    logger.info("Initializing Neo4j indexes...")

    neo4j_client = Neo4jClient()
    neo4j_client.create_indexes()

    logger.info("Neo4j indexes created successfully")

if __name__ == "__main__":
    main()
