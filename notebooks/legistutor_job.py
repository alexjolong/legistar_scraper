# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # legistutor: check, refresh, summarize
# MAGIC
# MAGIC Runs the same commands as the `legistutor` command line, against a Unity Catalog volume.
# MAGIC Schedule this notebook as a job with `command` = `refresh`. Run it once by hand with
# MAGIC `command` = `check` first: it tries the model endpoints, the volume and the outside
# MAGIC sites, and says what is missing.

# COMMAND ----------

# MAGIC %pip install -q -r ../requirements-job.txt

# COMMAND ----------

dbutils.library.restartPython()  # noqa: F821 - provided by the notebook

# COMMAND ----------

dbutils.widgets.text("data_dir", "/Volumes/sbx_ida_workshop/alex_legistar_rag/data/", "Data directory (UC volume)")  # noqa: F821
dbutils.widgets.dropdown("command", "check", ["check", "refresh", "refresh --no-law", "index --full", "summarize-law", "status"], "Command")  # noqa: F821
dbutils.widgets.text("embed_endpoint", "databricks-qwen3-embedding-0-6b", "Embedding endpoint")  # noqa: F821
dbutils.widgets.text("chat_endpoint", "databricks-claude-sonnet-5-5", "Chat endpoint")  # noqa: F821
dbutils.widgets.text("plural_policy_api_key_path", "", "3-part Unity Catalog path locating PLURAL_POLICY_API_KEY (optional)")  # noqa: F821

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.abspath("../src"))

os.environ["LEGISTUTOR_PROVIDER"] = "databricks"
os.environ["LEGISTUTOR_EMBED_MODEL"] = dbutils.widgets.get("embed_endpoint")  # noqa: F821
os.environ["LEGISTUTOR_CHAT_MODEL"] = dbutils.widgets.get("chat_endpoint")  # noqa: F821

# State bills come from the Open States API, which needs a key. Without one, bills are skipped.
secret_path = [part.strip() for part in dbutils.widgets.get("plural_policy_api_key_path").split(".") if part.strip()]  # noqa: F821
if len(secret_path) == 3:
    catalog, schema, key = secret_path
    os.environ["PLURAL_POLICY_API_KEY"] = dbutils.secrets.get(catalog=catalog, schema=schema, key=key)  # noqa: F821
elif secret_path:
    raise ValueError("plural_policy_api_key_path must be catalog.schema.key, the three-part name of a Unity Catalog secret")

# COMMAND ----------

from legistutor.cli import main

arguments = ["--data-dir", dbutils.widgets.get("data_dir"), *dbutils.widgets.get("command").split()]  # noqa: F821
status = main(arguments)
if status:
    raise RuntimeError(f"legistutor {' '.join(arguments[2:])} finished with status {status}; see the output above")