"""GraphFrames over the silver graph, for batch graph analytics.

Optional: needs the ``spark`` extra (``uv sync --extra spark``) and a JDK that
Spark supports (17 or 21). The rest of the pipeline never imports this module.

Interactive lookups should use :mod:`legistar_scraper.query` (DuckDB) instead:
starting a Spark session takes several seconds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .silver import silver_root

if TYPE_CHECKING:
    from graphframes import GraphFrame
    from pyspark.sql import SparkSession

# The Python package is only the API; the engine is these JARs (GraphFrames and its
# own GraphX build, compiled against Spark 4.1). Keep the version in step with
# graphframes-py, and the Spark line in step with pyspark in pyproject.toml.
GRAPHFRAMES_VERSION = "0.12.3"
GRAPHFRAMES_ARTIFACTS = ("graphframes-spark4_2.13", "graphframes-graphx-spark4_2.13")
MAVEN_BASE = "https://repo1.maven.org/maven2/io/graphframes"
JAR_CACHE = Path.home() / ".cache" / "legistar_scraper" / "jars"


def ensure_jars() -> list[Path]:
    """Download the GraphFrames JARs from Maven Central once, checking each against
    its published SHA-1, and return their local paths.

    They go on the driver classpath directly rather than through
    ``spark.jars.packages``: that route makes Spark chmod the files, which on
    Windows needs Hadoop's ``winutils.exe``.
    """
    import hashlib

    import httpx

    JAR_CACHE.mkdir(parents=True, exist_ok=True)
    jars = []
    for artifact in GRAPHFRAMES_ARTIFACTS:
        name = f"{artifact}-{GRAPHFRAMES_VERSION}.jar"
        path = JAR_CACHE / name
        if not path.exists():
            url = f"{MAVEN_BASE}/{artifact}/{GRAPHFRAMES_VERSION}/{name}"
            content = httpx.get(url, follow_redirects=True, timeout=120).raise_for_status().content
            expected = httpx.get(url + ".sha1", follow_redirects=True, timeout=60).raise_for_status().text.split()[0]
            if hashlib.sha1(content).hexdigest() != expected:
                raise RuntimeError(f"Checksum mismatch for {url}")
            path.write_bytes(content)
        jars.append(path)
    return jars


def find_java_home() -> str | None:
    """A JDK Spark can run on: ``LEGISTAR_JAVA_HOME`` if set, else a JDK 17 or 21
    under ``~/.jdks`` (where this project's setup puts one), else whatever
    ``JAVA_HOME`` already says."""
    explicit = os.environ.get("LEGISTAR_JAVA_HOME")
    if explicit:
        return explicit
    for version in ("17", "21"):
        for candidate in sorted(Path.home().glob(f".jdks/*{version}.*"), reverse=True):
            if (candidate / "bin").is_dir():
                return str(candidate)
    return os.environ.get("JAVA_HOME")


def spark_session(app_name: str = "legistar-graph", driver_memory: str = "2g") -> SparkSession:
    """A local Spark session sized for a laptop, with GraphFrames on the classpath."""
    from pyspark.sql import SparkSession

    java_home = find_java_home()
    if java_home:
        os.environ["JAVA_HOME"] = java_home
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    return (
        SparkSession.builder.master("local[*]")
        .appName(app_name)
        .config("spark.driver.extraClassPath", os.pathsep.join(str(jar) for jar in ensure_jars()))
        .config("spark.driver.memory", driver_memory)
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.sql.shuffle.partitions", "8")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )


def load_graph(spark: SparkSession, data_dir: Path, client: str) -> GraphFrame:
    """The whole graph: vertices ``(id, type, label)``, edges ``(src, dst, relationship, date)``.

    For attributes, join a typed table back on ``id``, e.g.
    ``spark.read.parquet(str(silver_root(data_dir, client) / "vertices" / "matter.parquet"))``.
    """
    from graphframes import GraphFrame

    root = silver_root(data_dir, client).resolve() / "graph"
    vertices = spark.read.parquet((root / "vertices.parquet").as_uri())
    edges = spark.read.parquet((root / "edges.parquet").as_uri())
    return GraphFrame(vertices, edges)


def demo(data_dir: Path, client: str) -> None:
    """Smoke test and worked example: degree counts, a motif, and PageRank."""
    from pyspark.sql import functions as F

    spark = spark_session()
    spark.sparkContext.setLogLevel("ERROR")
    try:
        graph = load_graph(spark, data_dir, client).cache()
        print(f"vertices: {graph.vertices.count()}  edges: {graph.edges.count()}")
        graph.edges.groupBy("relationship").count().orderBy(F.desc("count")).show(20, truncate=False)

        print("Most-cited statutes (in-degree over 'cites' edges):")
        cites = graph.filterEdges("relationship = 'cites'")
        (
            cites.inDegrees.join(graph.vertices, "id")
            .select("label", "inDegree")
            .orderBy(F.desc("inDegree"))
            .show(8, truncate=False)
        )

        print("Motif: matters a department requested that cite a statute, by department and chapter:")
        motif = graph.find("(m)-[r]->(d); (m)-[c]->(s); (s)-[p]->(ch)").filter(
            "r.relationship = 'requested_by' AND c.relationship = 'cites' AND p.relationship = 'part_of'"
        )
        (
            motif.groupBy(F.col("d.label").alias("department"), F.col("ch.label").alias("chapter"))
            .agg(F.countDistinct("m.id").alias("matters"))
            .orderBy(F.desc("matters"))
            .show(8, truncate=False)
        )

        print("PageRank over matter-to-matter links (references and related_to):")
        links = graph.filterEdges("relationship IN ('references', 'related_to')").dropIsolatedVertices()
        ranked = links.pageRank(resetProbability=0.15, maxIter=10)
        ranked.vertices.select("label", F.round("pagerank", 2).alias("pagerank")).orderBy(F.desc("pagerank")).show(
            8, truncate=90
        )
    finally:
        spark.stop()
