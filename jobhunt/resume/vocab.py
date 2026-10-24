"""Shared technology vocabulary.

Lives on its own so both the ATS keyword scorer and the truthfulness
validator can use it without importing each other.
"""

from __future__ import annotations

# Technology and methodology vocabulary. Coverage is scored only over terms in
# here (plus the candidate's own declared skills), because this is what an ATS
# keyword-matches and what a resume can honestly answer.
TECH_VOCAB = frozenset(
    ["python", "java", "javascript", "typescript", "go", "golang", "rust", "kotlin", "swift", "scala", "ruby", "php", "perl", "c", "c++", "c#", ".net", "objective-c", "dart", "elixir", "erlang", "haskell", "clojure", "lua", "matlab", "bash", "shell", "powershell", "sql", "nosql", "plsql", "tsql", "react", "reactjs", "angular", "angularjs", "vue", "vuejs", "svelte", "nextjs", "nuxt", "remix", "jquery", "redux", "node", "nodejs", "express", "nestjs", "django", "flask", "fastapi", "rails", "spring", "springboot", "hibernate", "laravel", "symfony", "dotnet", "graphql", "grpc", "rest", "soap", "openapi", "swagger", "websocket", "webrtc", "html", "css", "sass", "scss", "tailwind", "bootstrap", "webpack", "vite", "babel", "eslint", "android", "ios", "flutter", "xamarin", "postgres", "postgresql", "mysql", "mariadb", "sqlite", "oracle", "mssql", "mongodb", "cassandra", "dynamodb", "redis", "memcached", "elasticsearch", "opensearch", "neo4j", "couchdb", "influxdb", "clickhouse", "snowflake", "bigquery", "redshift", "databricks", "spark", "hadoop", "hive", "kafka", "rabbitmq", "activemq", "pulsar", "sqs", "sns", "kinesis", "flink", "airflow", "dbt", "etl", "elt", "warehouse", "lakehouse", "aws", "azure", "gcp", "oci", "cloud", "ec2", "s3", "lambda", "ecs", "eks", "fargate", "rds", "cloudformation", "kubernetes", "k8s", "docker", "containers", "containerd", "helm", "istio", "terraform", "ansible", "puppet", "chef", "openshift", "nomad", "consul", "vault", "serverless", "microservices", "monolith", "ci", "cd", "cicd", "jenkins", "gitlab", "github", "bitbucket", "circleci", "argocd", "argo", "spinnaker", "git", "prometheus", "grafana", "datadog", "newrelic", "splunk", "sentry", "opentelemetry", "observability", "logging", "monitoring", "alerting", "sre", "devops", "linux", "unix", "windows", "macos", "nginx", "apache", "tomcat", "haproxy", "envoy", "pytest", "junit", "jest", "mocha", "cypress", "playwright", "selenium", "testng", "cucumber", "appium", "tdd", "bdd", "unit", "integration", "e2e", "regression", "qa", "automation", "agile", "scrum", "kanban", "jira", "confluence", "ml", "ai", "llm", "llms", "nlp", "genai", "rag", "embeddings", "vector", "transformers", "pytorch", "tensorflow", "keras", "scikit", "sklearn", "pandas", "numpy", "scipy", "huggingface", "langchain", "langgraph", "openai", "anthropic", "inference", "finetuning", "training", "recommendation", "ranking", "agents", "agentic", "tooling", "mcp", "oauth", "oidc", "saml", "jwt", "sso", "ldap", "kerberos", "rbac", "iam", "encryption", "tls", "ssl", "pki", "security", "authentication", "authorization", "compliance", "soc2", "gdpr", "hipaa", "pci", "api", "apis", "backend", "frontend", "fullstack", "full-stack", "distributed", "scalability", "scalable", "latency", "throughput", "caching", "sharding", "replication", "concurrency", "multithreading", "async", "architecture", "design", "patterns", "algorithms", "optimization", "performance", "migration", "refactoring", "debugging", "profiling"]
)


# --------------------------------------------------------------------------
# Synonyms
# --------------------------------------------------------------------------
#
# A job description saying "K8s" against a resume saying "Kubernetes" is a
# match to every human reader and to any competent ATS, but a substring test
# scores it as a miss. That error runs both ways: the term is also counted as
# something the profile lacks, so a skill the candidate genuinely has shows up under
# "asked for but not in your profile".
#
# Only genuinely interchangeable spellings belong here. A false grouping
# inflates the coverage number, which is the one thing the ATS check exists to
# report honestly - "docker" and "kubernetes" are related, not equivalent, so
# they are not grouped.
SYNONYMS: tuple[frozenset[str], ...] = (
    frozenset({"kubernetes", "k8s"}),
    frozenset({"postgres", "postgresql", "psql"}),
    frozenset({"javascript", "js"}),
    frozenset({"typescript", "ts"}),
    frozenset({"node", "nodejs", "node.js"}),
    frozenset({"react", "reactjs", "react.js"}),
    frozenset({"vue", "vuejs", "vue.js"}),
    frozenset({"angular", "angularjs"}),
    frozenset({"nextjs", "next.js"}),
    frozenset({"go", "golang"}),
    frozenset({"c#", "csharp", "c-sharp"}),
    frozenset({"c++", "cpp"}),
    frozenset({".net", "dotnet"}),
    frozenset({"spring", "springboot", "spring-boot"}),
    frozenset({"cicd", "ci/cd", "ci-cd"}),
    frozenset({"mongodb", "mongo"}),
    frozenset({"elasticsearch", "elastic"}),
    frozenset({"microservices", "micro-services"}),
    frozenset({"rest", "restful"}),
    frozenset({"oauth", "oauth2"}),
    frozenset({"sso", "single-sign-on"}),
    frozenset({"sre", "site-reliability"}),
    frozenset({"e2e", "end-to-end"}),
    frozenset({"qa", "quality-assurance"}),
    frozenset({"ml", "machine-learning"}),
    frozenset({"ai", "artificial-intelligence"}),
    frozenset({"llm", "llms", "large-language-model", "large-language-models"}),
    frozenset({"genai", "generative-ai"}),
    frozenset({"rag", "retrieval-augmented-generation"}),
    frozenset({"scikit", "sklearn", "scikit-learn"}),
    frozenset({"pytorch", "torch"}),
    frozenset({"huggingface", "hugging-face"}),
    frozenset({"openapi", "swagger"}),
    frozenset({"websocket", "websockets"}),
    frozenset({"gcp", "google-cloud"}),
    frozenset({"aws", "amazon-web-services"}),
    frozenset({"oci", "oracle-cloud"}),
    frozenset({"fullstack", "full-stack"}),
    frozenset({"api", "apis"}),
)

# member -> the group it belongs to, built once.
_SYNONYM_INDEX: dict[str, frozenset[str]] = {
    member: group for group in SYNONYMS for member in group
}


def equivalents(term: str) -> frozenset[str]:
    """Every spelling that means the same thing as ``term``.

    Returns the term itself when it has no recorded synonyms, so callers can
    use this unconditionally.
    """
    low = term.lower().strip()
    return _SYNONYM_INDEX.get(low, frozenset({low}))


def matches_any(term: str, haystack: str) -> bool:
    """Does ``haystack`` contain ``term`` under any of its spellings?"""
    return any(spelling in haystack for spelling in equivalents(term))


def known_to(term: str, vocabulary: set[str]) -> bool:
    """Is ``term`` in ``vocabulary`` under any of its spellings?"""
    return bool(equivalents(term) & vocabulary)
