import asyncio
import json
import logging
from sqlalchemy import select, delete, text

from app.db.session import AsyncSessionLocal
from app.db.models import PayrollRoutingMatrix, PayrollWorkflowConfig, PayrollWorkflowQueue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("remove_variable_pay")

async def main():
    async with AsyncSessionLocal() as db:
        # 1. Delete from payroll_routing_matrix
        logger.info("Purging VARIABLE PAY from payroll_routing_matrix...")
        matrix_del = await db.execute(
            delete(PayrollRoutingMatrix).where(PayrollRoutingMatrix.earning_head == "VARIABLE PAY")
        )
        logger.info(f"Deleted matrix rules count: {matrix_del.rowcount}")

        # 2. Delete active queues for VARIABLE PAY (if any)
        logger.info("Purging VARIABLE PAY from payroll_workflow_queue...")
        queue_del = await db.execute(
            delete(PayrollWorkflowQueue).where(PayrollWorkflowQueue.module == "VARIABLE PAY")
        )
        logger.info(f"Deleted queue sheets count: {queue_del.rowcount}")

        # 3. Remove from workflow config JSON
        logger.info("Updating active_config in payroll_workflow_config...")
        res = await db.execute(
            select(PayrollWorkflowConfig).where(PayrollWorkflowConfig.key == "active_config")
        )
        cfg_record = res.scalar_one_or_none()
        if cfg_record:
            config_data = cfg_record.config
            if isinstance(config_data, dict) and "earning_heads" in config_data:
                original_heads = config_data["earning_heads"]
                updated_heads = [h for h in original_heads if h.get("name") != "VARIABLE PAY"]
                if len(original_heads) != len(updated_heads):
                    config_data["earning_heads"] = updated_heads
                    # Mark as modified for SQLAlchemy to detect changes on JSONB
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(cfg_record, "config")
                    logger.info("VARIABLE PAY removed from earning_heads list in active_config.")
                else:
                    logger.info("VARIABLE PAY not found in active_config earning_heads list.")
            else:
                logger.info("active_config is empty or not in expected dict structure.")
        else:
            logger.warning("active_config record not found in payroll_workflow_config.")

        await db.commit()
        logger.info("Database commit completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
