"""Seed data used to pre-populate the features table on first run.

The "Workflows" feature mirrors the keyword list the scraper originally used
(workflows/render_workflows_keywords.csv), so the app starts with one working
feature to manage instead of an empty screen.
"""

SEED_FEATURES = [
    {
        "name": "Workflows",
        "documentation_url": "https://render.com/docs/workflows",
        "keywords": [
            "AI agent", "LLM pipeline", "machine learning", "model inference",
            "vector database", "embedding", "fine-tuning", "RAG", "GPT",
            "OpenAI", "Anthropic", "Hugging Face", "computer vision", "NLP",
            "ETL", "data pipeline", "data ingestion", "data processing",
            "batch processing", "data transformation", "Apache Spark", "dbt",
            "Airflow", "Kafka", "data warehouse", "Snowflake", "BigQuery",
            "Redshift", "Fivetran", "Databricks", "background job", "job queue",
            "task queue", "async worker", "worker process", "cron job",
            "scheduled task", "Celery", "Sidekiq", "BullMQ", "Redis queue",
            "message queue", "RabbitMQ", "SQS", "auto-scaling",
            "distributed computing", "parallel processing", "horizontal scaling",
            "microservices", "serverless", "event-driven",
            "workflow orchestration", "orchestration", "task orchestration",
            "Python", "TypeScript", "Node.js", "FastAPI", "Django",
            "async/await", "asyncio", "document processing", "video processing",
            "image processing", "report generation", "web scraping",
            "data enrichment", "email automation", "notification system",
            "content generation", "data export", "PDF generation", "OCR",
            "data sync",
        ],
    },
]
