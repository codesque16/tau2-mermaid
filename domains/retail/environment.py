from typing import Optional
from data_model import RetailDB
from tools import RetailTools
from environment import Environment
from utils import load_file


def get_environment(
    db: Optional[RetailDB] = None,
    solo_mode: bool = False,
) -> Environment:
    if solo_mode:
        raise ValueError("Retail domain does not support solo mode")
    if db is None:
        db = RetailDB.load("db.json")
    tools = RetailTools(db)
    with open("policy.md", "r") as fp:
        policy = fp.read()
    return Environment(
        domain_name="retail",
        policy=policy,
        tools=tools,
    )


def get_tasks() -> list[Task]:
    tasks = load_file("tasks.json")
    return tasks