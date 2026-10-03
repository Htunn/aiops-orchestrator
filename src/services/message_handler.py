"""Core message handling service."""

import asyncio
import re
import uuid
from datetime import UTC
from typing import Any

import structlog

from src.ai import AIRouter, ContextBuilder, ModelSelector, PromptManager
from src.channels.base import ChannelMessage
from src.channels.router import MessageRouter
from src.database import get_db_session
from src.database.redis import RedisCache, get_redis
from src.monitoring.tracing import get_tracer
from src.services.azure_handler import AzureHandler
from src.services.platform_handler import PlatformHandler
from src.services.session_manager import SessionManager

logger = structlog.get_logger()
_tracer = get_tracer(__name__)

# Kubernetes keywords for detection
K8S_KEYWORDS = [
    "pod",
    "pods",
    "deployment",
    "deployments",
    "service",
    "services",
    "namespace",
    "namespaces",
    "node",
    "nodes",
    "kubectl",
    "k8s",
    "kubernetes",
    "helm",
    "container",
    "containers",
    "scale",
    "configmap",
    "secret",
    "ingress",
    "pvc",
    "persistentvolume",
    "statefulset",
    "daemonset",
    # AIOps self-healing verbs
    "restart",
    "rollback",
    "drain",
    "cordon",
    "uncordon",
    "taint",
    "evict",
    "analyze logs",
    "crashloop",
    "crashlooping",
    "crashing",
    "oomkill",
    "oom killed",
    "not ready",
    "notready",
    "patch resource",
    "runbook",
    "playbook",
    "rca",
    "root cause",
    "remediat",
]

# Platform management keywords for detection
PLATFORM_KEYWORDS = [
    "vm",
    "vms",
    "virtual machine",
    "virtual machines",
    "nutanix",
    "vmware",
    "vcenter",
    "esxi",
    "prism",
    "openshift",
    "host",
    "hosts",
    "hypervisor",
    "cluster",
    "datacenter",
    "infrastructure",
]


class MessageHandler:
    """Handles incoming messages and orchestrates responses."""

    def __init__(
        self,
        router: MessageRouter,
        ai_client: AIRouter,
    ):
        self.router = router
        self.ai_client = ai_client
        self.approval_manager = None  # Set by main.py after AIOps init
        self.task_delegator = None  # Set by main.py if A2A enabled
        self.platform_handler = PlatformHandler()  # Platform operations handler
        self.azure_handler = AzureHandler()  # Azure ARM operations handler (SPEC-002)
        logger.info("message_handler_initialized")

    def _format_kubectl_table(self, output: str, resource_type: str = "pods") -> str:
        """
        Format kubectl table output for better readability in chat.

        Args:
            output: Raw kubectl output
            resource_type: Type of resource (pods, nodes, deployments, etc.)

        Returns:
            Formatted string for chat display
        """
        lines = output.strip().split("\n")
        if len(lines) <= 1:
            return output

        # Parse header and rows
        header_line = lines[0]
        data_lines = lines[1:]

        # For pods, show key information in a compact format
        if resource_type == "pods":
            formatted = []
            # Detect --all-namespaces output: first column header is NAMESPACE
            has_ns_col = header_line.strip().upper().startswith("NAMESPACE")
            for line in data_lines:
                parts = line.split()
                min_cols = 6 if has_ns_col else 5
                if len(parts) >= min_cols:
                    if has_ns_col:
                        ns = parts[0]
                        name = parts[1]
                        ready = parts[2]
                        status = parts[3]
                        restarts = parts[4]
                        age = parts[5]
                    else:
                        ns = None
                        name = parts[0]
                        ready = parts[1]
                        status = parts[2]
                        restarts = parts[3]
                        age = parts[4]

                    # Status emoji
                    status_emoji = "✅" if status == "Running" and "/" in ready else "⚠️"
                    if status in ["CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull"]:
                        status_emoji = "❌"
                    elif status in ["Pending", "ContainerCreating"]:
                        status_emoji = "⏳"
                    elif status == "Completed":
                        status_emoji = "✔️"
                    elif status == "OOMKilled":
                        status_emoji = "💥"

                    name_label = f"`{ns}/{name}`" if ns else f"**{name}**"
                    formatted.append(
                        f"{status_emoji} {name_label}\n"
                        f"   Status: {status} | Ready: {ready} | Restarts: {restarts} | Age: {age}"
                    )

            return "\n\n".join(formatted) if formatted else "No resources found"

        # For nodes, show compact format
        elif resource_type == "nodes":
            formatted = []
            for line in data_lines:
                parts = line.split()
                if len(parts) >= 5:
                    name = parts[0]
                    status = parts[1]
                    roles = parts[2]
                    age = parts[3]
                    version = parts[4]

                    status_emoji = "✅" if status == "Ready" else "❌"
                    formatted.append(
                        f"{status_emoji} **{name}**\n   Status: {status} | Role: {roles} | Version: {version} | Age: {age}"
                    )

            return "\n\n".join(formatted) if formatted else "No nodes found"

        # For deployments, show compact format
        elif resource_type == "deployments":
            formatted = []
            for line in data_lines:
                parts = line.split()
                if len(parts) >= 4:
                    name = parts[0]
                    ready = parts[1]
                    up_to_date = parts[2]
                    available = parts[3]
                    age = parts[4] if len(parts) > 4 else "N/A"

                    # Check if deployment is healthy
                    status_emoji = "✅" if "/" in ready else "⚠️"
                    try:
                        current, desired = ready.split("/")
                        if current != desired:
                            status_emoji = "⚠️"
                    except Exception:
                        pass

                    formatted.append(
                        f"{status_emoji} **{name}**\n   Ready: {ready} | Up-to-date: {up_to_date} | Available: {available} | Age: {age}"
                    )

            return "\n\n".join(formatted) if formatted else "No deployments found"

        # For services and other resources, use table format but truncate
        else:
            # Keep header and limit column widths
            formatted = [f"```\n{header_line}"]
            for line in data_lines[:20]:  # Limit to 20 rows
                formatted.append(line)

            if len(data_lines) > 20:
                formatted.append(f"... and {len(data_lines) - 20} more")

            formatted.append("```")
            return "\n".join(formatted)

    async def _run_kubectl_command(self, args: list[str]) -> tuple[bool, str]:
        """
        Run a kubectl command and return the output.

        Args:
            args: kubectl command arguments (without 'kubectl' prefix)

        Returns:
            Tuple of (success, output)
        """
        try:
            cmd = ["kubectl"] + args
            logger.info("running_kubectl", command=" ".join(cmd))

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                output = stdout.decode("utf-8").strip()
                return True, output
            else:
                error = stderr.decode("utf-8").strip()
                logger.error("kubectl_command_failed", error=error, returncode=process.returncode)
                return False, error

        except FileNotFoundError:
            logger.error("kubectl_not_found")
            return (
                False,
                "kubectl command not found. Please ensure kubectl is installed and in your PATH.",
            )
        except Exception as e:
            logger.error("kubectl_command_error", error=str(e))
            return False, f"Error executing kubectl command: {str(e)}"

    async def handle_message(self, message: ChannelMessage) -> None:
        """
        Handle incoming message from any channel.

        Args:
            message: Incoming channel message
        """
        with _tracer.start_as_current_span(
            "message.handle",
            attributes={
                "channel.type": message.channel_type,
                "message.length": len(message.content),
            },
        ):
            logger.info(
                "message_received",
                channel_type=message.channel_type,
                user_id=message.user_id,
                content_length=len(message.content),
            )

            # ── Normalise command prefix: '!' is the Slack-friendly alternative to '/'
            # Slack intercepts messages starting with '/' and shows "not a valid command".
            # Users can type '!k8s pods', '!help', etc. from Slack.
            if message.content.startswith("!"):
                message.content = "/" + message.content[1:]

            # ── AIOps: check if this is an approval response first ────
            if self.approval_manager and not message.content.startswith("/"):
                try:
                    result = await self.approval_manager.process_response(
                        message.content, message.user_id, message.user_id
                    )
                    if result is not None:
                        # Send the approval_manager response back via router
                        await self.router.send_message(
                            message.channel_type, message.user_id, result
                        )
                        return  # message was an approval command
                except Exception as e:
                    logger.warning("approval_process_response_error", error=str(e))

            # ── A2A: check if this is an agent delegation request ────
            # Syntax: @agent-name do something or @capability: parameters
            if self.task_delegator and message.content.startswith("@"):
                await self._handle_agent_delegation(message)
                return

            # Check for commands
            if message.content.startswith("/"):
                await self._handle_command(message)
                return

            # Check for a contextual K8s follow-up FIRST — before keyword detection.
            # Phrases like "show details" / "pls show details of pods" contain K8s keywords
            # ("pod") but are really follow-ups to the previous query; we must resolve the
            # cached namespace and pass an explicit query so the right filter is applied.
            k8s_followup_content = await self._get_k8s_followup_query(message)
            if k8s_followup_content:
                followup_msg = ChannelMessage(
                    content=k8s_followup_content,
                    user_id=message.user_id,
                    username=message.username,
                    channel_type=message.channel_type,
                    raw_event=message.raw_event,
                )
                await self._handle_kubernetes_query(followup_msg)
                return

            # Check if it's a Kubernetes-related query
            if self._is_kubernetes_query(message.content):
                await self._handle_kubernetes_query(message)
                return

            # Check if it's an Azure resource management query (SPEC-002)
            if self.azure_handler.is_azure_query(message.content):
                await self._handle_azure_query(message)
                return

            # Check if it's a platform management query
            if self._is_platform_query(message.content):
                await self._handle_platform_query(message)
                return

            # Process regular message
            await self._process_message(message)

    def _is_kubernetes_query(self, message_text: str) -> bool:
        """Check if message is related to Kubernetes."""
        message_lower = message_text.lower()
        return any(keyword in message_lower for keyword in K8S_KEYWORDS)

    async def _handle_kubernetes_query(self, message: ChannelMessage) -> None:
        """Handle Kubernetes-related queries."""
        logger.info(
            "kubernetes_query_detected",
            channel_type=message.channel_type,
            user_id=message.user_id,
            query=message.content,
        )

        # Parse natural language query
        query_lower = message.content.lower()
        response = None

        # Extract namespace if mentioned
        namespace = None
        namespace_patterns = [
            r"(?:in|from|on)\s+(?:the\s+)?([a-z0-9\-]+)(?:\s+namespace)?",
            r"([a-z0-9\-]+)\s+namespace",  # "pos-order4u namespace" - name before "namespace" keyword
            r"namespace\s+([a-z0-9\-]+)",
            r"-n\s+([a-z0-9\-]+)",
        ]
        for pattern in namespace_patterns:
            match = re.search(pattern, query_lower)
            if match:
                namespace = match.group(1)
                # Skip if it's a common kubernetes keyword, not a namespace
                if namespace not in [
                    "pod",
                    "pods",
                    "deployment",
                    "service",
                    "node",
                    "container",
                    "the",
                    "check",
                    "show",
                    "list",
                    "get",
                    "and",
                ]:
                    break
                namespace = None

        # Detect intent and execute appropriate command
        try:
            # ── Word-number normalisation ("one replica" → "1 replica") ─────────
            _WORD_NUMS = {
                "zero": "0",
                "one": "1",
                "two": "2",
                "three": "3",
                "four": "4",
                "five": "5",
                "six": "6",
                "seven": "7",
                "eight": "8",
                "nine": "9",
                "ten": "10",
            }
            normalized = query_lower
            for _w, _d in _WORD_NUMS.items():
                normalized = re.sub(rf"\b{_w}\b", _d, normalized)

            # ── Scale / resize — checked FIRST, before pod/deployment branches ───
            # Handles: "scale down superadmin-frontend pod to one replica"
            #          "scale my-app to 2 replicas"  "resize web to 0 replicas" etc.
            if any(kw in normalized for kw in ("scale", "resize")) and re.search(
                r"\d+\s+replica", normalized
            ):
                num_match = re.search(r"(\d+)\s+replica", normalized)
                # Extract deployment name: first hyphenated token after scale/resize verb
                name_match = re.search(
                    r"(?:scale\s+(?:down\s+|up\s+)?|resize\s+)([a-z0-9][a-z0-9\-\.]+)",
                    normalized,
                )
                if not name_match:
                    # fallback: hyphenated word before pod/deployment/to N replica
                    name_match = re.search(
                        r"([a-z0-9][a-z0-9\-\.]{3,})(?:\s+(?:pod|deployment|app|service))?\s+(?:to\s+)?\d+\s+replica",
                        normalized,
                    )
                if name_match and num_match:
                    deployment = name_match.group(1).rstrip("-.")
                    replicas = num_match.group(1)

                    # Auto-discover namespace when not specified:
                    # run 'kubectl get deployment -A' and find the line matching the name.
                    resolved_ns = namespace
                    if not resolved_ns:
                        ok, all_deps = await self._run_kubectl_command(
                            ["get", "deployment", "--all-namespaces"]
                        )
                        if ok:
                            for _line in all_deps.splitlines()[1:]:
                                _parts = _line.split()
                                if len(_parts) >= 2 and _parts[1] == deployment:
                                    resolved_ns = _parts[0]
                                    break

                    kubectl_args = ["scale", "deployment", deployment, f"--replicas={replicas}"]
                    kubectl_args.extend(["-n", resolved_ns] if resolved_ns else [])
                    success, output = await self._run_kubectl_command(kubectl_args)
                    if success:
                        ns_label = f" in namespace `{resolved_ns}`" if resolved_ns else ""
                        response = f"⚖️ Scaled `{deployment}`{ns_label} → `{replicas}` replica(s)\n```\n{output}\n```"
                    else:
                        response = (
                            f"❌ Could not scale `{deployment}` to `{replicas}` replica(s):\n```\n{output}\n```\n\n"
                            "Use `!k8s deployments` to verify the deployment name and namespace."
                        )
                else:
                    response = (
                        "❌ Could not parse deployment name or replica count.\n\n"
                        "**Examples:**\n"
                        "• _scale superadmin-frontend to 1 replica_\n"
                        "• _scale down my-app pod to 2 replicas_\n"
                        "• `/k8s scale <name> <count> [namespace]`"
                    )

            # ── Self-healing: fix / clean up / remediate error pods ────────────
            # Catch BEFORE the generic pod-listing block so "fix these pods"
            # doesn't fall through to list logic.
            elif any(
                kw in query_lower
                for kw in (
                    "fix ",
                    "fix these",
                    "fix the",
                    "fix error",
                    "fix issue",
                    "fix pod",
                    "fix crash",
                    "fix oom",
                    "clean up pod",
                    "cleanup pod",
                    "clean pod",
                    "remediat",
                    "delete error",
                    "delete failed",
                    "remove error",
                    "remove failed",
                    "resolve pod",
                )
            ):
                response = await self._fix_problem_pods(namespace)

            # List pods
            elif any(word in query_lower for word in ["pod", "pods", "container", "containers"]):
                if "log" in query_lower:
                    # Extract pod name
                    pod_match = re.search(r"(?:pod|container)\s+(\S+)", query_lower)
                    if pod_match:
                        pod_name = pod_match.group(1)
                        kubectl_args = ["logs", pod_name]
                        if namespace:
                            kubectl_args.extend(["-n", namespace])
                        else:
                            kubectl_args.extend(["-n", "default"])

                        success, output = await self._run_kubectl_command(kubectl_args)
                        if success:
                            lines = output.split("\n")
                            if len(lines) > 50:
                                output = (
                                    "\n".join(lines[-50:])
                                    + f"\n\n(Showing last 50 lines of {len(lines)} total)"
                                )
                            response = f"📜 **Logs from pod {pod_name}:**\n\n```\n{output}\n```"
                        else:
                            response = f"❌ Error getting logs: {output}"
                    else:
                        response = "❌ Please specify a pod name. Example: 'show logs from pod nginx-abc123'"
                else:
                    # Detect status filter
                    status_filter = None
                    filter_description = ""

                    if any(
                        word in query_lower
                        for word in [
                            "error",
                            "errors",
                            "failed",
                            "failing",
                            "crash",
                            "crashloop",
                            "crashloopbackoff",
                        ]
                    ):
                        status_filter = "problem"
                        filter_description = " with issues"
                    elif any(
                        word in query_lower for word in ["unhealthy", "not ready", "notready"]
                    ):
                        status_filter = "notready"
                        filter_description = " not ready"
                    elif "pending" in query_lower:
                        status_filter = "pending"
                        filter_description = " pending"
                    elif any(word in query_lower for word in ["running", "healthy", "ready"]):
                        status_filter = "running"
                        filter_description = " running"
                    elif any(
                        word in query_lower
                        for word in ["all", "detail", "details", "everything", "full"]
                    ):
                        status_filter = "all"
                        filter_description = ""

                    # List pods
                    kubectl_args = ["get", "pods", "-o", "wide"]
                    if namespace:
                        kubectl_args.extend(["-n", namespace])
                    else:
                        kubectl_args.append("--all-namespaces")

                    success, output = await self._run_kubectl_command(kubectl_args)
                    if success:
                        # Apply status filtering if requested
                        if status_filter:
                            lines = output.split("\n")
                            header = lines[0] if lines else ""
                            filtered_lines = [header]

                            for line in lines[1:]:
                                if not line.strip():
                                    continue

                                if status_filter == "problem":
                                    # Show pods that are not Running or Completed
                                    if any(
                                        status in line
                                        for status in [
                                            "Error",
                                            "CrashLoopBackOff",
                                            "ImagePullBackOff",
                                            "ErrImagePull",
                                            "Pending",
                                            "Failed",
                                            "Unknown",
                                            "Terminating",
                                            "ContainerCreating",
                                            "OOMKilled",
                                        ]
                                    ):
                                        filtered_lines.append(line)
                                    # Also check for Running pods with restarts
                                    elif "Running" in line:
                                        parts = line.split()
                                        # Check READY column (usually 2nd or 3rd column depending on namespace)
                                        for part in parts:
                                            if "/" in part:
                                                ready, total = part.split("/")
                                                if ready != total:  # Not all containers ready
                                                    filtered_lines.append(line)
                                                    break
                                elif status_filter == "notready":
                                    # Show pods where READY != total or status not Running
                                    if "Running" not in line or any(
                                        status in line
                                        for status in ["Pending", "Error", "CrashLoop"]
                                    ):
                                        filtered_lines.append(line)
                                    else:
                                        parts = line.split()
                                        for part in parts:
                                            if "/" in part:
                                                ready, total = part.split("/")
                                                if ready != total:
                                                    filtered_lines.append(line)
                                                    break
                                elif status_filter == "pending":
                                    if "Pending" in line or "ContainerCreating" in line:
                                        filtered_lines.append(line)
                                elif status_filter == "running":
                                    if "Running" in line:
                                        # Check if all containers are ready
                                        parts = line.split()
                                        is_healthy = False
                                        for part in parts:
                                            if "/" in part:
                                                ready, total = part.split("/")
                                                if ready == total:
                                                    is_healthy = True
                                                break
                                        if is_healthy:
                                            filtered_lines.append(line)
                                elif status_filter == "all":
                                    filtered_lines.append(line)

                            if len(filtered_lines) > 1:
                                output = "\n".join(filtered_lines)
                                formatted_output = self._format_kubectl_table(output, "pods")
                                response = f"📦 **Pods{filter_description}{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n{formatted_output}"
                            else:
                                response = f"✅ **No pods{filter_description} found{f' in namespace {namespace}' if namespace else ' (all namespaces)'}**\n\nAll pods appear to be running normally! 🎉"
                        else:
                            # No explicit filter requested — show only problem pods to keep
                            # Slack replies concise.  Show "all healthy" summary if none.
                            lines = output.split("\n")
                            header = lines[0] if lines else ""
                            _bad = [
                                "Error",
                                "CrashLoopBackOff",
                                "ImagePullBackOff",
                                "Pending",
                                "Failed",
                                "Unknown",
                                "Terminating",
                                "ContainerCreating",
                                "OOMKilled",
                            ]
                            problem_lines: list[str] = []
                            healthy_count = 0
                            for _line in lines[1:]:
                                if not _line.strip():
                                    continue
                                if any(s in _line for s in _bad):
                                    problem_lines.append(_line)
                                elif "Running" in _line:
                                    _parts = _line.split()
                                    _degraded = False
                                    for _p in _parts:
                                        if "/" in _p:
                                            _r, _t = _p.split("/", 1)
                                            if _r != _t:
                                                _degraded = True
                                            break
                                    if _degraded:
                                        problem_lines.append(_line)
                                    else:
                                        healthy_count += 1
                            ns_label = (
                                f" in namespace `{namespace}`" if namespace else " (all namespaces)"
                            )
                            if problem_lines:
                                _filt = "\n".join([header] + problem_lines)
                                formatted_output = self._format_kubectl_table(_filt, "pods")
                                _fix_hint = (
                                    "\n\n💡 _Type **`fix pods`** or **`!k8s fix`**"
                                    + (f" (or `!k8s fix {namespace}`)" if namespace else "")
                                    + " to auto-remediate Error/CrashLoop/OOMKilled pods._"
                                )
                                response = (
                                    f"⚠️ **Problem pods{ns_label}** "
                                    f"({len(problem_lines)} issue(s), {healthy_count} healthy):\n\n"
                                    f"{formatted_output}{_fix_hint}"
                                )
                            else:
                                response = f"✅ All {healthy_count} pod(s){ns_label} are healthy."
                    else:
                        response = f"❌ Error getting pods: {output}"

            # List deployments
            elif any(word in query_lower for word in ["deployment", "deployments", "deploy"]):
                if "scale" in query_lower:
                    # Extract deployment name and replica count
                    deploy_match = re.search(r"(?:deployment|deploy)\s+(\S+)", query_lower)
                    replica_match = re.search(
                        r"(?:to\s+)?(\d+)\s+(?:replica|replicas|instance)", query_lower
                    )

                    if deploy_match and replica_match:
                        deployment = deploy_match.group(1)
                        replicas = replica_match.group(1)
                        kubectl_args = ["scale", "deployment", deployment, f"--replicas={replicas}"]
                        if namespace:
                            kubectl_args.extend(["-n", namespace])
                        else:
                            kubectl_args.extend(["-n", "default"])

                        success, output = await self._run_kubectl_command(kubectl_args)
                        if success:
                            response = f"⚖️ **Scaled deployment {deployment} to {replicas} replicas:**\n\n{output}"
                        else:
                            response = f"❌ Error scaling deployment: {output}"
                    else:
                        response = "❌ Please specify deployment name and replica count. Example: 'scale api-server deployment to 3 replicas'"
                else:
                    # List deployments
                    kubectl_args = ["get", "deployments", "-o", "wide"]
                    if namespace:
                        kubectl_args.extend(["-n", namespace])
                    else:
                        kubectl_args.append("--all-namespaces")

                    success, output = await self._run_kubectl_command(kubectl_args)
                    if success:
                        formatted_output = self._format_kubectl_table(output, "deployments")
                        response = f"🚀 **Deployments{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n{formatted_output}"
                    else:
                        response = f"❌ Error getting deployments: {output}"

            # List services
            elif any(word in query_lower for word in ["service", "services", "svc"]):
                kubectl_args = ["get", "services", "-o", "wide"]
                if namespace:
                    kubectl_args.extend(["-n", namespace])
                else:
                    kubectl_args.append("--all-namespaces")

                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    response = f"🌐 **Services{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n```\n{output}\n```"
                else:
                    response = f"❌ Error getting services: {output}"

            # List nodes
            elif any(word in query_lower for word in ["node", "nodes", "cluster"]):
                success, output = await self._run_kubectl_command(["get", "nodes", "-o", "wide"])
                if success:
                    response = f"🖥️ **Nodes:**\n\n```\n{output}\n```"
                else:
                    response = f"❌ Error getting nodes: {output}"

            # List namespaces
            elif "namespace" in query_lower and not namespace:
                success, output = await self._run_kubectl_command(["get", "namespaces"])
                if success:
                    response = f"🏢 **Namespaces:**\n\n```\n{output}\n```"
                else:
                    response = f"❌ Error getting namespaces: {output}"

            # Show events
            elif any(
                word in query_lower
                for word in ["event", "events", "what happened", "what's happening"]
            ):
                kubectl_args = ["get", "events"]
                if namespace:
                    kubectl_args.extend(["-n", namespace])
                else:
                    kubectl_args.append("--all-namespaces")

                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    response = f"📰 **Events{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n```\n{output}\n```"
                else:
                    response = f"❌ Error getting events: {output}"

            # ── Self-healing: restart pod ──────────────────────────────────
            elif any(word in query_lower for word in ["restart"]):
                pod_match = re.search(r"restart\s+(?:pod\s+)?(\S+)", query_lower)
                deploy_match = re.search(r"restart\s+(?:deployment\s+)?(\S+)", query_lower)
                if pod_match:
                    pod_name = pod_match.group(1)
                    kubectl_args = ["delete", "pod", pod_name, "--grace-period=0"]
                    if namespace:
                        kubectl_args.extend(["-n", namespace])
                    else:
                        kubectl_args.extend(["-n", "default"])
                    success, output = await self._run_kubectl_command(kubectl_args)
                    response = (
                        f"♻️ Restarted pod `{pod_name}`: {output}"
                        if success
                        else f"❌ Error restarting pod: {output}"
                    )
                elif deploy_match:
                    deploy_name = deploy_match.group(1)
                    kubectl_args = [
                        "rollout",
                        "restart",
                        f"deployment/{deploy_name}",
                    ]
                    if namespace:
                        kubectl_args.extend(["-n", namespace])
                    success, output = await self._run_kubectl_command(kubectl_args)
                    response = (
                        f"♻️ Rolling restart of deployment `{deploy_name}`: {output}"
                        if success
                        else f"❌ Error restarting deployment: {output}"
                    )
                else:
                    response = "❌ Please specify what to restart. Example: 'restart pod nginx-abc123' or 'restart deployment my-app'"

            # ── Self-healing: rollback deployment ─────────────────────────
            elif "rollback" in query_lower:
                deploy_match = re.search(r"rollback\s+(?:deployment\s+)?(\S+)", query_lower)
                revision_match = re.search(r"to\s+revision\s+(\d+)", query_lower)
                if deploy_match:
                    deploy_name = deploy_match.group(1)
                    kubectl_args = ["rollout", "undo", f"deployment/{deploy_name}"]
                    if revision_match:
                        kubectl_args.extend([f"--to-revision={revision_match.group(1)}"])
                    if namespace:
                        kubectl_args.extend(["-n", namespace])
                    success, output = await self._run_kubectl_command(kubectl_args)
                    response = (
                        f"⏪ Rolled back deployment `{deploy_name}`: {output}"
                        if success
                        else f"❌ Error rolling back: {output}"
                    )
                else:
                    response = "❌ Please specify deployment to roll back. Example: 'rollback deployment my-app'"

            # ── Self-healing: cordon / uncordon / drain node ───────────────
            elif any(word in query_lower for word in ["cordon", "uncordon", "drain"]):
                node_match = re.search(
                    r"(?:cordon|uncordon|drain)\s+(?:node\s+)?(\S+)", query_lower
                )
                if node_match:
                    node_name = node_match.group(1)
                    if "uncordon" in query_lower:
                        kubectl_args = ["uncordon", node_name]
                        action = "Uncordoned"
                    elif "drain" in query_lower:
                        kubectl_args = [
                            "drain",
                            node_name,
                            "--ignore-daemonsets",
                            "--delete-emissary-data",
                            "--timeout=120s",
                        ]
                        action = "Drained"
                    else:
                        kubectl_args = ["cordon", node_name]
                        action = "Cordoned"
                    success, output = await self._run_kubectl_command(kubectl_args)
                    response = (
                        f"🔒 {action} node `{node_name}`: {output}"
                        if success
                        else f"❌ Error: {output}"
                    )
                else:
                    response = "❌ Please specify a node name. Example: 'drain node worker-1'"

            # ── Self-healing: show CrashLoop pods ─────────────────────────
            elif any(
                word in query_lower for word in ["crashloop", "crashlooping", "crashing", "oom"]
            ):
                bad_statuses = ["CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff"]
                kubectl_args = ["get", "pods", "--all-namespaces", "-o", "wide"]
                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    lines = output.split("\n")
                    header = lines[0] if lines else ""
                    problem_lines = [header] + [
                        line for line in lines[1:] if any(s in line for s in bad_statuses)
                    ]
                    if len(problem_lines) > 1:
                        response = (
                            f"🚨 **Problematic Pods:**\n\n```\n{chr(10).join(problem_lines)}\n```"
                        )
                    else:
                        response = "✅ No CrashLoop / OOMKilled pods found."
                else:
                    response = f"❌ Error: {output}"

            # ── Explicit "fix" / "remediate" without pod/crash keyword ───────
            # e.g. "fix the cluster" / "clean up failed resources"
            elif any(kw in query_lower for kw in ("fix", "clean", "remediat", "repair")):
                response = await self._fix_problem_pods(namespace)

            # Default: show help
            else:
                response = (
                    "🔧 **Kubernetes Integration**\n\n"
                    "I couldn't understand your query.\n\n"
                    "*Natural language examples:*\n"
                    "• _show pods in pos-order4u namespace_\n"
                    "• _scale down superadmin-frontend to 1 replica_\n"
                    "• _restart pod nginx-abc123_\n"
                    "• _show error pods_\n"
                    "• _fix these issue pods_ — auto-remediate crashloop/error pods\n"
                    "• _fix pods in velero namespace_\n\n"
                    "*Explicit commands (Slack — use `!` instead of `/`):*\n"
                    "• `!k8s pods [namespace]`\n"
                    "• `!k8s fix [namespace]` — auto-remediate error pods\n"
                    "• `!k8s deployments [namespace]`\n"
                    "• `!k8s scale <name> <replicas> [namespace]`\n"
                    "• `!k8s logs <pod> [namespace]`\n"
                    "• `!k8s nodes`\n\n"
                    "*AIOps:*\n"
                    "• `!incident list` — open incidents\n"
                    "• `!alert list` — recent alerts"
                )

        except Exception as e:
            logger.error("k8s_query_error", error=str(e), query=message.content)
            response = f"❌ Error: {str(e)}\n\nTry `!k8s help` for all commands."

        # Send response
        await self.router.send_message(message.channel_type, message.user_id, response)

        # Persist exchange so follow-up messages have conversation context
        await self._persist_exchange(message, response, model_used="kubectl")
        # Cache the resolved namespace so follow-up queries (e.g. "show details") can reuse it
        await self._store_k8s_context(message.channel_type, message.user_id, namespace)

    def _is_platform_query(self, message_text: str) -> bool:
        """Check if message is related to platform management."""
        message_lower = message_text.lower()

        # Check for platform keywords
        if not any(keyword in message_lower for keyword in PLATFORM_KEYWORDS):
            return False

        # Must also contain an action verb to avoid false positives
        action_verbs = [
            "start",
            "stop",
            "restart",
            "reboot",
            "list",
            "show",
            "get",
            "status",
            "power",
        ]
        has_action = any(verb in message_lower for verb in action_verbs)

        return has_action

    async def _handle_platform_query(self, message: ChannelMessage) -> None:
        """Handle platform management queries."""
        logger.info(
            "platform_query_detected",
            channel_type=message.channel_type,
            user_id=message.user_id,
            query=message.content,
        )

        try:
            # Extract user ID
            user_id = uuid.UUID(message.user_id) if message.user_id else None

            # Execute platform command
            result = await self.platform_handler.execute_command(
                command=message.content,
                user_id=user_id,
            )

            # Format response
            if result.get("success"):
                operation = result.get("operation", "operation")

                if operation == "list_vms":
                    # Format VM list nicely
                    total = result.get("total", 0)
                    running = result.get("running", 0)
                    stopped = result.get("stopped", 0)
                    vms = result.get("vms", [])
                    platforms = result.get("platforms_checked", [])

                    response = "🖥️ **Virtual Machines**\n\n"
                    response += (
                        f"**Summary:** {total} total ({running} running, {stopped} stopped)\n"
                    )
                    response += f"**Platforms:** {', '.join(platforms)}\n\n"

                    if vms:
                        # Group by platform
                        by_platform: dict[str, list[dict[str, Any]]] = {}
                        for vm in vms:
                            platform = vm.get("platform", "unknown")
                            if platform not in by_platform:
                                by_platform[platform] = []
                            by_platform[platform].append(vm)

                        # Display grouped by platform
                        for platform, platform_vms in by_platform.items():
                            response += f"**{platform.upper()}:**\n"
                            for vm in platform_vms[:10]:  # Limit to 10 per platform
                                state_emoji = "✅" if vm.get("power_state") == "running" else "⚪"
                                response += f"{state_emoji} **{vm['name']}**\n"
                                response += f"   State: {vm['power_state']} | "
                                if vm.get("cpu_count"):
                                    response += f"CPUs: {vm['cpu_count']} | "
                                if vm.get("memory_mb"):
                                    response += f"Memory: {vm['memory_mb']}MB | "
                                if vm.get("cluster"):
                                    response += f"Cluster: {vm['cluster']}"
                                response += "\n\n"

                            if len(platform_vms) > 10:
                                response += f"   _...and {len(platform_vms) - 10} more VMs_\n\n"
                    else:
                        response += "No VMs found.\n"

                elif operation == "list_hosts":
                    # Format host list nicely
                    total = result.get("total", 0)
                    healthy = result.get("healthy", 0)
                    unhealthy = result.get("unhealthy", 0)
                    hosts = result.get("hosts", [])
                    platforms = result.get("platforms_checked", [])

                    response = "🖥️ **Hosts/Nodes**\n\n"
                    response += (
                        f"**Summary:** {total} total ({healthy} healthy, {unhealthy} unhealthy)\n"
                    )
                    response += f"**Platforms:** {', '.join(platforms)}\n\n"

                    if hosts:
                        for host in hosts[:15]:  # Limit to 15 hosts
                            state_emoji = "✅" if host.get("status") == "ready" else "❌"
                            response += f"{state_emoji} **{host['name']}**\n"
                            response += (
                                f"   Status: {host['status']} | Platform: {host['platform']}"
                            )
                            if host.get("cpu_capacity"):
                                response += f" | CPUs: {host['cpu_capacity']}"
                            if host.get("memory_capacity_mb"):
                                response += f" | Memory: {host['memory_capacity_mb']}MB"
                            response += "\n\n"

                        if len(hosts) > 15:
                            response += f"_...and {len(hosts) - 15} more hosts_\n"
                    else:
                        response += "No hosts found.\n"

                else:
                    # Generic success response for operations like start/stop/restart
                    vm_name = result.get("vm_name", "VM")
                    platform = result.get("platform", "unknown")

                    response = (
                        f"✅ **{result.get('message', 'Operation completed successfully')}**\n\n"
                    )
                    response += f"**Platform:** {platform}\n"
                    response += f"**VM:** {vm_name}\n"
                    response += f"**Operation:** {operation.replace('_', ' ').title()}\n"

                    if result.get("force"):
                        response += "**Mode:** Force shutdown\n"

            else:
                # Error response
                error = result.get("error", "Unknown error")
                response = f"❌ **Platform operation failed**\n\n{error}"

            logger.info("platform_query_processed", success=result.get("success"))

        except Exception as e:
            logger.error("platform_query_failed", error=str(e), exc_info=True)
            response = f"❌ **Error processing platform query:** {str(e)}"

        await self.router.send_message(message.channel_type, message.user_id, response)

    async def _handle_azure_query(self, message: ChannelMessage) -> None:
        """Handle an Azure ARM resource management query (SPEC-002)."""
        logger.info(
            "azure_query_detected",
            channel_type=message.channel_type,
            user_id=message.user_id,
            query=message.content,
        )

        async def _send(channel_target: str, text: str) -> bool:
            return await self.router.send_message(message.channel_type, channel_target, text)

        response = await self.azure_handler.handle_query(
            message.content,
            requested_by=message.user_id,
            channel_type=message.channel_type,
            channel_target=message.user_id,
            send_message_callback=_send,
        )

        await self.router.send_message(message.channel_type, message.user_id, response)

    async def _persist_exchange(
        self, message: ChannelMessage, response: str, model_used: str = "system"
    ) -> None:
        """Persist a user/assistant exchange to the conversation history DB."""
        try:
            async with get_db_session() as db_session:
                redis_cache = RedisCache(get_redis())
                session_mgr = SessionManager(redis_cache, db_session)
                context_builder = ContextBuilder(db_session)
                session_data = await session_mgr.get_or_create_session(
                    message.channel_type, message.user_id, message.username
                )
                conversation_id = uuid.UUID(session_data.conversation_id)
                await context_builder.add_user_message(conversation_id, message.content)
                await context_builder.add_assistant_message(
                    conversation_id, response, model_used=model_used, token_count=None
                )
                await session_mgr.update_session_activity(message.channel_type, message.user_id)
                await session_mgr.increment_message_count(message.channel_type, message.user_id)
        except Exception as e:
            logger.warning("persist_exchange_failed", error=str(e))

    async def _store_k8s_context(
        self, channel_type: str, user_id: str, namespace: str | None
    ) -> None:
        """Cache the last K8s namespace in Redis so follow-up queries can reuse it."""
        try:
            redis_cache = RedisCache(get_redis())
            context_key = f"k8s-ctx:{channel_type}:{user_id}"
            await redis_cache.set(context_key, namespace or "", ttl=1800)  # 30-minute TTL
        except Exception as e:
            logger.warning("store_k8s_context_failed", error=str(e))

    async def _get_k8s_followup_query(self, message: ChannelMessage) -> str | None:
        """Return an augmented K8s query if the message is a follow-up to a prior K8s response.

        Detects short phrases like "show details", "can you pls show details of pods",
        "list all", "show more", etc. and resolves them against the last cached K8s namespace.
        Returns None if the message is not a recognised follow-up.
        """
        FOLLOWUP_PATTERNS = [
            r"\b(show|list|get|see|display)\b.{0,30}\b(detail|details|pods?|them|all|more|everything)\b",
            r"^(detail|details|more info|show more|show all|list all|all of them|list them)\s*$",
            r"\b(can you|could you|please|pls)\b.{0,40}\b(show|list|get)\b.{0,30}\b(detail|pods?|more|all|them|everything)\b",
        ]
        msg_lower = message.content.lower().strip()
        if not any(re.search(p, msg_lower) for p in FOLLOWUP_PATTERNS):
            return None
        # If the message already contains an explicit namespace reference, it is a self-contained
        # query — not a follow-up. Let it be handled by normal K8s routing so it uses the
        # namespace the user typed, not a cached one.
        NAMESPACE_INDICATORS = [
            r"(?:in|from|on)\s+(?:the\s+)?[a-z0-9][a-z0-9\-]+\s+namespace",
            r"\bnamespace\s+[a-z0-9][a-z0-9\-]+",
            r"-n\s+[a-z0-9][a-z0-9\-]+",
        ]
        if any(re.search(pat, msg_lower) for pat in NAMESPACE_INDICATORS):
            return None
        try:
            redis_cache = RedisCache(get_redis())
            context_key = f"k8s-ctx:{message.channel_type}:{message.user_id}"
            namespace = await redis_cache.get(context_key)
            if namespace is None:
                # No prior K8s context — let the AI handle it
                return None
            if namespace:
                # Include "details" so the pod-listing branch uses status_filter="all"
                return f"show all pods details in {namespace} namespace"
            return "show all pods details"
        except Exception:
            return None

    async def _handle_command(self, message: ChannelMessage) -> None:
        """Handle command messages."""
        command_parts = message.content.split()
        # Normalise: /k8s and !k8s are equivalent; accept bare 'k8s' too
        command = command_parts[0].lower()
        if not command.startswith("/"):
            command = "/" + command.lstrip("!")

        logger.info("command_received", command=command, parts=command_parts)

        async with get_db_session() as db_session:
            session_mgr = SessionManager(RedisCache(get_redis()), db_session)
            session_data = await session_mgr.get_or_create_session(
                message.channel_type, message.user_id, message.username
            )

            if command == "/help":
                response = PromptManager.get_command_help()

            elif command == "/reset":
                await session_mgr.clear_session(message.channel_type, message.user_id)
                response = "Conversation reset! Starting fresh."

            elif command == "/status":
                context_builder = ContextBuilder(db_session)
                stats = await context_builder.get_message_stats(
                    uuid.UUID(session_data.conversation_id)
                )
                model_selector = ModelSelector(db_session)
                current_model = await model_selector.select_model(
                    uuid.UUID(session_data.user_id),
                    uuid.UUID(session_data.conversation_id),
                    message.channel_type,
                )
                response = f"""📊 Status:
Model: {current_model}
Messages: {stats["message_count"]}
Tokens: {stats["total_tokens"]}"""

            elif command == "/model":
                if len(command_parts) < 2:
                    response = "Usage: /model <gpt-4|claude-3-opus|llama-3-70b>"
                else:
                    new_model = command_parts[1]
                    if self.ai_client.is_model_supported(new_model):
                        model_selector = ModelSelector(db_session)
                        await model_selector.set_user_model(
                            uuid.UUID(session_data.user_id), new_model
                        )
                        response = f"Model set to: {new_model}"
                    else:
                        supported = ", ".join(self.ai_client.list_supported_models())
                        response = f"Unsupported model. Available: {supported}"

            elif command == "/k8s":
                logger.info("k8s_command_received", args=command_parts[1:])
                try:
                    response = await self._handle_k8s_command(
                        command_parts[1:] if len(command_parts) > 1 else []
                    )
                    logger.info("k8s_command_processed", response_length=len(response))
                except Exception as e:
                    logger.error("k8s_command_failed", error=str(e), error_type=type(e).__name__)
                    response = f"Error processing Kubernetes command: {str(e)}"

            elif command == "/approval":
                response = await self._handle_approval_command(command_parts[1:], message)

            elif command == "/incident":
                response = await self._handle_incident_command(command_parts[1:])

            elif command == "/alert":
                response = await self._handle_alert_command(command_parts[1:])

            elif command == "/a2a":
                response = await self._handle_a2a_command(
                    command_parts[1:] if len(command_parts) > 1 else []
                )

            else:
                response = (
                    "Unknown command. Try `!help`\n\n"
                    "*Tip for Slack users:* prefix commands with `!` instead of `/`\n"
                    "e.g. `!k8s pods`, `!k8s scale <name> <n>`, `!status`"
                )

            # Send response
            await self.router.send_message(message.channel_type, message.user_id, response)

    async def _handle_k8s_command(self, args: list[str]) -> str:
        """
        Handle Kubernetes commands.

        Args:
            args: Command arguments (everything after /k8s)

        Returns:
            Response message
        """
        if not args or args[0] == "help":
            return """🔧 Kubernetes Commands

Pod Management:
• /k8s pods - List all pods
• /k8s pods <namespace> - List pods in namespace
• /k8s fix - Auto-remediate Error/CrashLoop/OOMKilled pods (all namespaces)
• /k8s fix <namespace> - Fix problem pods in a specific namespace
• /k8s describe pod <name> [namespace] - Get pod details
• /k8s logs <pod-name> [namespace] - Get pod logs
• /k8s top pods - Show pod resource usage

Deployment Management:
• /k8s deployments [namespace] - List deployments
• /k8s scale <deployment> <replicas> [namespace] - Scale deployment
• /k8s rollout status <deployment> [namespace] - Check rollout status

Service Management:
• /k8s services [namespace] - List services
• /k8s endpoints [namespace] - List endpoints

Node Management:
• /k8s nodes - List nodes
• /k8s top nodes - Show node resource usage
• /k8s describe node <name> - Get node details

Namespace Management:
• /k8s namespaces - List all namespaces

Helm:
• /k8s helm list - List Helm releases
• /k8s helm status <release> - Get Helm release status

Events & Logs:
• /k8s events [namespace] - Show recent events
• /k8s logs <pod> [namespace] - Get pod logs

Configuration:
• /k8s contexts - List available contexts
• /k8s config - View current configuration

Examples:
  /k8s pods production
  /k8s logs nginx-abc123 production
  /k8s scale api-server 5 production
  /k8s nodes
  /k8s deployments

Note: Kubernetes MCP tools are integrated. You can manage your cluster directly from this chat!
"""

        subcommand = args[0].lower()

        try:
            # Handle different subcommands with kubectl
            if subcommand == "pods":
                namespace = args[1] if len(args) > 1 else None
                logger.info("k8s_listing_pods", namespace=namespace)

                kubectl_args = ["get", "pods"]
                if namespace:
                    kubectl_args.extend(["-n", namespace])
                else:
                    kubectl_args.append("--all-namespaces")

                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    formatted_output = self._format_kubectl_table(output, "pods")
                    return f"📦 **Pods{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n{formatted_output}"
                else:
                    return f"❌ Error getting pods: {output}"

            elif subcommand == "nodes":
                logger.info("k8s_listing_nodes")
                success, output = await self._run_kubectl_command(["get", "nodes"])
                if success:
                    formatted_output = self._format_kubectl_table(output, "nodes")
                    return f"🖥️ **Nodes:**\n\n{formatted_output}"
                else:
                    return f"❌ Error getting nodes: {output}"

            elif subcommand == "namespaces":
                logger.info("k8s_listing_namespaces")
                success, output = await self._run_kubectl_command(["get", "namespaces"])
                if success:
                    return f"🏢 **Namespaces:**\n\n```\n{output}\n```"
                else:
                    return f"❌ Error getting namespaces: {output}"

            elif subcommand == "deployments":
                namespace = args[1] if len(args) > 1 else None
                logger.info("k8s_listing_deployments", namespace=namespace)
                kubectl_args = ["get", "deployments"]
                if namespace:
                    kubectl_args.extend(["-n", namespace])
                else:
                    kubectl_args.append("--all-namespaces")
                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    formatted_output = self._format_kubectl_table(output, "deployments")
                    return f"🚀 **Deployments{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n{formatted_output}"
                else:
                    return f"❌ Error getting deployments: {output}"

            elif subcommand == "services":
                namespace = args[1] if len(args) > 1 else None
                logger.info("k8s_listing_services", namespace=namespace)
                kubectl_args = ["get", "services"]
                if namespace:
                    kubectl_args.extend(["-n", namespace])
                else:
                    kubectl_args.append("--all-namespaces")
                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    return f"🌐 **Services{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n```\n{output}\n```"
                else:
                    return f"❌ Error getting services: {output}"

            elif subcommand == "contexts":
                logger.info("k8s_listing_contexts")
                success, output = await self._run_kubectl_command(["config", "get-contexts"])
                if success:
                    return f"🔧 **Contexts:**\n\n```\n{output}\n```"
                else:
                    return f"❌ Error getting contexts: {output}"

            elif subcommand == "logs":
                if len(args) < 2:
                    return "❌ Usage: /k8s logs <pod-name> [namespace]"
                pod_name = args[1]
                namespace = args[2] if len(args) > 2 else "default"
                logger.info("k8s_getting_logs", pod=pod_name, namespace=namespace)
                success, output = await self._run_kubectl_command(
                    ["logs", pod_name, "-n", namespace]
                )
                if success:
                    # Limit log output to last 50 lines for readability
                    lines = output.split("\n")
                    if len(lines) > 50:
                        output = (
                            "\n".join(lines[-50:])
                            + f"\n\n(Showing last 50 lines of {len(lines)} total)"
                        )
                    return f"📜 **Logs from pod {pod_name} in namespace {namespace}:**\n\n```\n{output}\n```"
                else:
                    return f"❌ Error getting logs: {output}"

            elif subcommand == "scale":
                if len(args) < 3:
                    return "❌ Usage: /k8s scale <deployment> <replicas> [namespace]"
                deployment = args[1]
                replicas = args[2]
                namespace = args[3] if len(args) > 3 else "default"
                logger.info(
                    "k8s_scaling_deployment",
                    deployment=deployment,
                    replicas=replicas,
                    namespace=namespace,
                )
                success, output = await self._run_kubectl_command(
                    ["scale", "deployment", deployment, f"--replicas={replicas}", "-n", namespace]
                )
                if success:
                    return f"⚖️ **Scaling deployment {deployment} to {replicas} replicas in namespace {namespace}:**\n\n{output}"
                else:
                    return f"❌ Error scaling deployment: {output}"

            elif subcommand == "events":
                namespace = args[1] if len(args) > 1 else None
                logger.info("k8s_listing_events", namespace=namespace)
                kubectl_args = ["get", "events"]
                if namespace:
                    kubectl_args.extend(["-n", namespace])
                else:
                    kubectl_args.append("--all-namespaces")
                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    return f"📰 **Events{f' in namespace {namespace}' if namespace else ' (all namespaces)'}:**\n\n```\n{output}\n```"
                else:
                    return f"❌ Error getting events: {output}"

            elif subcommand == "describe":
                if len(args) < 3:
                    return "❌ Usage: /k8s describe <resource-type> <name> [namespace]"
                resource_type = args[1]
                name = args[2]
                namespace = args[3] if len(args) > 3 else "default"
                logger.info(
                    "k8s_describe", resource_type=resource_type, name=name, namespace=namespace
                )

                kubectl_args = ["describe", resource_type, name]
                # Don't add namespace for cluster-scoped resources like nodes
                if resource_type.lower() not in ["node", "nodes", "namespace", "namespaces"]:
                    kubectl_args.extend(["-n", namespace])

                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    return f"🔍 **{resource_type} {name}:**\n\n```\n{output}\n```"
                else:
                    return f"❌ Error describing {resource_type}: {output}"

            elif subcommand == "helm":
                if len(args) < 2:
                    return "❌ Usage: /k8s helm <list|status|...> [args...]"
                helm_command = args[1]
                logger.info("k8s_helm_command", command=helm_command)

                if helm_command == "list":
                    success, output = await self._run_kubectl_command(
                        ["get", "all", "-A", "-l", "app.kubernetes.io/managed-by=Helm"]
                    )
                    if success:
                        return f"⎈ **Helm-managed Resources:**\n\n```\n{output}\n```\n\n_Note: For full Helm functionality, install helm CLI and use: helm list --all-namespaces_"
                    else:
                        return f"❌ Error listing Helm resources: {output}"
                else:
                    return f"⎈ Helm command '{helm_command}' requires helm CLI. This bot focuses on kubectl commands.\n\nFor Helm: install helm and run: `helm {helm_command}`"

            elif subcommand == "top":
                if len(args) < 2:
                    return "❌ Usage: /k8s top <pods|nodes> [namespace]"
                resource = args[1]
                logger.info("k8s_top", resource=resource)

                kubectl_args = ["top", resource]
                if resource == "pods" and len(args) > 2:
                    kubectl_args.extend(["-n", args[2]])
                elif resource == "pods":
                    kubectl_args.append("--all-namespaces")

                success, output = await self._run_kubectl_command(kubectl_args)
                if success:
                    namespace_info = (
                        f" in namespace {args[2]}" if resource == "pods" and len(args) > 2 else ""
                    )
                    return f"📊 **{resource.capitalize()} Resource Usage{namespace_info}:**\n\n```\n{output}\n```"
                else:
                    if "metrics-server" in output.lower():
                        return "❌ Metrics Server not available. Install it with:\n```\nkubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml\n```"
                    return f"❌ Error getting resource usage: {output}"

            elif subcommand == "config":
                logger.info("k8s_viewing_config")
                success, output = await self._run_kubectl_command(["config", "view"])
                if success:
                    # Limit output for security - don't show full config in chat
                    lines = output.split("\n")[:20]
                    truncated = "\n".join(lines)
                    return f"📋 **Kubernetes Configuration (first 20 lines):**\n\n```yaml\n{truncated}\n...\n```\n\n_For full config, use: kubectl config view_"
                else:
                    return f"❌ Error viewing config: {output}"

            elif subcommand == "fix":
                # /k8s fix [namespace]  — auto-remediate error/crash/failed pods
                fix_ns = args[1] if len(args) > 1 else None
                return await self._fix_problem_pods(fix_ns)

            else:
                return f"❌ Unknown Kubernetes command: {subcommand}\n\nTry `/k8s help` for available commands."

        except Exception as e:
            logger.error("k8s_command_error", error=str(e), subcommand=subcommand)
            return f"❌ Error executing Kubernetes command: {str(e)}\n\nPlease check your cluster configuration and try again."

    # ──────────────────────────────────────────────────────────────────────
    # Pod remediation helper
    # ──────────────────────────────────────────────────────────────────────

    async def _fix_problem_pods(self, namespace: str | None = None) -> str:
        """
        Auto-remediate problem pods across a namespace (or all namespaces).

        Strategy:
        - Error / Failed / Completed  → delete (job pods stay permanently; safe to remove)
        - OOMKilled                   → delete pod (triggers fresh start by controller)
        - CrashLoopBackOff            → delete pod (replicaset / daemonset recreates it)
        - ImagePullBackOff / ErrImage → report only (image tag / pull secret issue)
        - Pending / Unknown           → report only (scheduling / infra issue)
        """
        ns_label = f" in namespace `{namespace}`" if namespace else " (all namespaces)"

        kubectl_args = ["get", "pods", "-o", "wide"]
        if namespace:
            kubectl_args.extend(["-n", namespace])
        else:
            kubectl_args.append("--all-namespaces")

        success, output = await self._run_kubectl_command(kubectl_args)
        if not success:
            return f"❌ Could not list pods: {output}"

        lines = output.split("\n")
        header = lines[0] if lines else ""
        has_ns_col = header.strip().upper().startswith("NAMESPACE")

        # Categorise pods
        deletable: list[tuple[str, str, str]] = []  # (ns, name, status) — safe to delete
        manual: list[tuple[str, str, str]] = []  # needs human investigation

        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split()
            if has_ns_col:
                if len(parts) < 6:
                    continue
                pod_ns, pod_name, ready, status = parts[0], parts[1], parts[2], parts[3]
            else:
                if len(parts) < 5:
                    continue
                pod_ns = namespace or "default"
                pod_name = parts[0]
                ready = parts[1]
                status = parts[2]

            # Determine if this pod is a problem
            is_degraded = False
            if any(
                s in status
                for s in [
                    "Error",
                    "Failed",
                    "CrashLoopBackOff",
                    "OOMKilled",
                    "ImagePullBackOff",
                    "ErrImagePull",
                    "InvalidImageName",
                ]
            ):
                is_degraded = True
            elif status == "Unknown":
                is_degraded = True
            elif status == "Running" and "/" in ready:
                r, t = ready.split("/", 1)
                if r != t:
                    is_degraded = True

            if not is_degraded:
                continue

            # Route to correct bucket
            if status in (
                "Error",
                "Failed",
                "Completed",
                "OOMKilled",
                "CrashLoopBackOff",
                "Unknown",
            ):
                deletable.append((pod_ns, pod_name, status))
            else:
                # ImagePullBackOff, Pending, ContainerCreating — need manual fix
                manual.append((pod_ns, pod_name, status))

        if not deletable and not manual:
            return f"✅ No problem pods found{ns_label}!\nAll pods appear healthy. 🎉"

        fixed_lines: list[str] = []
        failed_lines: list[str] = []

        for pod_ns, pod_name, status in deletable:
            ok, out = await self._run_kubectl_command(
                ["delete", "pod", pod_name, "-n", pod_ns, "--grace-period=0"]
            )
            action = "Restarted" if status in ("CrashLoopBackOff",) else "Cleaned up"
            if ok:
                icon = "♻️" if "Restart" in action else "🗑️"
                suffix = " (controller will recreate it)" if status == "CrashLoopBackOff" else ""
                fixed_lines.append(
                    f"{icon} {action} `{pod_ns}/{pod_name}` (was `{status}`){suffix}"
                )
            else:
                failed_lines.append(
                    f"❌ Could not delete `{pod_ns}/{pod_name}` (`{status}`): {out[:80]}"
                )

        # Build response
        sections: list[str] = []
        total_fixed = len(fixed_lines)
        total_issues = len(deletable) + len(manual)

        sections.append(
            f"🔧 **Pod remediation{ns_label}:** {total_fixed}/{total_issues} issue(s) fixed"
        )

        if fixed_lines:
            sections.append("**Fixed:**\n" + "\n".join(fixed_lines))
        if failed_lines:
            sections.append("**Failed to fix:**\n" + "\n".join(failed_lines))
        if manual:
            manual_msgs = [
                f"⚠️ `{ns}/{name}` — `{st}` (needs manual investigation)\n"
                + (
                    f"   → Check pull secret / image tag: `kubectl describe pod {name} -n {ns}`"
                    if "Image" in st
                    else f"   → Investigate scheduling: `kubectl describe pod {name} -n {ns}`"
                )
                for ns, name, st in manual
            ]
            sections.append("**Need manual attention:**\n" + "\n".join(manual_msgs))

        if total_fixed > 0:
            sections.append("_Run `pods` or `!k8s pods` again in ~30 s to verify recovery._")

        return "\n\n".join(sections)

    # ──────────────────────────────────────────────────────────────────────
    # AIOps command handlers
    # ──────────────────────────────────────────────────────────────────────

    async def _handle_approval_command(self, args: list[str], message: ChannelMessage) -> str:
        """Handle /approval list|approve <id>|reject <id>."""
        if not self.approval_manager:
            return "⚠️ Approval manager is not initialised."

        sub = args[0].lower() if args else "list"

        if sub == "list":
            try:
                pending = await self.approval_manager.list_pending()
                if not pending:
                    return "✅ No pending approvals."
                lines = ["📋 **Pending Approvals:**\n"]
                for ap in pending:
                    risk_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(
                        ap.risk_level.value.upper() if hasattr(ap, "risk_level") else "MEDIUM", "⚠️"
                    )
                    short_id = ap.approval_id[:8]
                    lines.append(
                        f"{risk_icon} `{short_id}` — **{ap.description}**\n"
                        f"   Requested by: {ap.requested_by} | "
                        f"Requested: {ap.requested_at.strftime('%H:%M UTC')}"
                    )
                return "\n".join(lines)
            except Exception as e:
                return f"❌ Error listing approvals: {e}"

        elif sub in ("approve", "reject") and len(args) >= 2:
            short_id = args[1]
            action_text = f"{sub} {short_id}"
            try:
                result = await self.approval_manager.process_response(
                    action_text, message.user_id, message.user_id
                )
                if result is not None:
                    return result
                return f"⚠️ No pending approval found with ID `{short_id}`."
            except Exception as e:
                return f"❌ Error processing approval: {e}"
        else:
            return (
                "🔧 **Approval Commands:**\n"
                "• `/approval list` — list pending approvals\n"
                "• `/approval approve <id>` — approve a pending action\n"
                "• `/approval reject <id>` — reject a pending action\n\n"
                "You can also reply with `approve <id>` or `reject <id>` directly."
            )

    async def _handle_incident_command(self, args: list[str]) -> str:
        """Handle /incident list|show <id>|close <id>."""
        sub = args[0].lower() if args else "list"

        try:
            from sqlalchemy import text as sql_text

            from src.database.postgres import engine

            async with engine.connect() as conn:
                if sub == "list":
                    rows = await conn.execute(
                        sql_text(
                            "SELECT id, title, severity, status, event_type, namespace, created_at "
                            "FROM incidents WHERE status = 'open' ORDER BY created_at DESC LIMIT 20"
                        )
                    )
                    incidents = rows.fetchall()
                    if not incidents:
                        return "✅ No open incidents."
                    lines = ["🚨 **Open Incidents:**\n"]
                    sev_icon = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🔵"}
                    for inc in incidents:
                        icon = sev_icon.get(inc.severity, "⚪")
                        short_id = str(inc.id)[:8]
                        lines.append(
                            f"{icon} `{short_id}` [{inc.severity}] **{inc.title}**\n"
                            f"   Type: {inc.event_type} | NS: {inc.namespace or 'n/a'} | "
                            f"Since: {inc.created_at.strftime('%Y-%m-%d %H:%M') if inc.created_at else 'unknown'}"
                        )
                    return "\n".join(lines)

                elif sub == "show" and len(args) >= 2:
                    short_id = args[1]
                    row = await conn.execute(
                        sql_text(
                            "SELECT id, title, severity, status, event_type, namespace, "
                            "resource_kind, resource_name, root_cause, rca_confidence, created_at, resolved_at "
                            "FROM incidents WHERE id::text LIKE :pattern ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"pattern": f"{short_id}%"},
                    )
                    incident_row = row.fetchone()
                    if not incident_row:
                        return f"❌ No incident found matching `{short_id}`."
                    confidence_str = (
                        f" ({incident_row.rca_confidence:.0%})"
                        if incident_row.rca_confidence
                        else ""
                    )
                    return (
                        f"📋 **Incident `{str(incident_row.id)[:8]}`**\n\n"
                        f"**Title:** {incident_row.title}\n"
                        f"**Severity:** {incident_row.severity} | **Status:** {incident_row.status}\n"
                        f"**Type:** {incident_row.event_type}\n"
                        f"**Resource:** {incident_row.resource_kind}/{incident_row.resource_name} in `{incident_row.namespace or 'n/a'}`\n"
                        f"**Root Cause:** {incident_row.root_cause or 'Not yet determined'}{confidence_str}\n"
                        f"**Opened:** {incident_row.created_at}\n"
                        f"**Resolved:** {incident_row.resolved_at or 'Still open'}"
                    )

                elif sub == "close" and len(args) >= 2:
                    short_id = args[1]
                    from datetime import datetime

                    result = await conn.execute(
                        sql_text(
                            "UPDATE incidents SET status='resolved', resolved_at=:ts "
                            "WHERE id::text LIKE :pattern AND status='open'"
                        ),
                        {"pattern": f"{short_id}%", "ts": datetime.now(UTC)},
                    )
                    await conn.commit()
                    if result.rowcount:
                        return f"✅ Incident `{short_id}` marked as resolved."
                    return f"⚠️ No open incident found matching `{short_id}`."

                else:
                    return (
                        "🚨 **Incident Commands:**\n"
                        "• `/incident list` — show open incidents\n"
                        "• `/incident show <id>` — show incident details\n"
                        "• `/incident close <id>` — resolve an incident"
                    )

        except Exception as e:
            logger.error("incident_command_error", error=str(e))
            return f"❌ Error: {e}"

    async def _handle_alert_command(self, args: list[str]) -> str:
        """Handle /alert list."""
        sub = args[0].lower() if args else "list"

        try:
            from sqlalchemy import text as sql_text

            from src.database.postgres import engine

            async with engine.connect() as conn:
                if sub == "list":
                    rows = await conn.execute(
                        sql_text(
                            "SELECT rule_name, severity, status, source, fired_at "
                            "FROM alert_events ORDER BY fired_at DESC LIMIT 20"
                        )
                    )
                    alerts = rows.fetchall()
                    if not alerts:
                        return "✅ No recent alert events."
                    lines = ["📣 **Recent Alerts (last 20):**\n"]
                    sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
                    for a in alerts:
                        icon = sev_icon.get(a.severity, "⚪")
                        status_icon = "🔥" if a.status == "firing" else "✅"
                        lines.append(
                            f"{icon}{status_icon} **{a.rule_name}** [{a.severity}] "
                            f"via {a.source} — {a.fired_at.strftime('%m-%d %H:%M') if a.fired_at else 'n/a'}"
                        )
                    return "\n".join(lines)
                else:
                    return "📣 **Alert Commands:**\n• `/alert list` — show recent alerts"

        except Exception as e:
            logger.error("alert_command_error", error=str(e))
            return f"❌ Error: {e}"

    async def _handle_a2a_command(self, args: list[str]) -> str:
        """Handle A2A (Agent-to-Agent) commands."""
        try:
            from src.config import get_settings

            settings = get_settings()

            if not settings.a2a_enabled:
                return "❌ **A2A integration is disabled**\n\nContact your administrator to enable Agent-to-Agent integration."

            if not args or args[0] == "help":
                return """🤖 **Agent-to-Agent (A2A) Commands**

**List Agents:**
• `/a2a agents` - Show all registered agents
• `/a2a agents <capability>` - Filter by capability

**Agent Info:**
• `/a2a agent <agent-id>` - Get detailed agent information

**Delegate Tasks:**
• `@agent-name <task description>` - Natural language delegation
• `@capability: param1=val1, param2=val2` - Structured delegation

**Examples:**
```
/a2a agents kubernetes
@kubernetes.scale: namespace=prod, deployment=api, replicas=5
@k8s-operator scale my-app to 3 replicas
```

**Status:**
• `/a2a status` - Show A2A system status
"""

            elif args[0] == "agents":
                capability_filter = args[1] if len(args) > 1 else None
                from src.services.agent_registry import get_agent_registry

                registry = get_agent_registry()
                agents = await registry.list_agents(capability_filter=capability_filter)

                if not agents:
                    return (
                        f"❌ No agents found with capability: `{capability_filter}`"
                        if capability_filter
                        else "❌ No agents registered yet."
                    )

                lines = [f"🤖 **Registered Agents** ({len(agents)} total)\n"]
                for agent in agents:
                    status_emoji = {
                        "online": "🟢",
                        "offline": "🔴",
                        "degraded": "🟡",
                        "unknown": "⚪",
                    }.get(
                        agent.status.value if hasattr(agent.status, "value") else agent.status, "⚪"
                    )
                    cap_count = len(agent.capabilities)
                    cap_names = [c.name for c in agent.capabilities[:3]]
                    cap_display = ", ".join(cap_names)
                    if cap_count > 3:
                        cap_display += f", +{cap_count - 3} more"
                    lines.append(
                        f"{status_emoji} **{agent.name}** (`{agent.agent_id}`)\n"
                        f"   • Capabilities: {cap_display}\n"
                        f"   • Status: {agent.status.value if hasattr(agent.status, 'value') else agent.status}\n"
                    )
                return "\n".join(lines)

            elif args[0] == "agent" and len(args) > 1:
                agent_id = args[1]
                from src.services.agent_registry import get_agent_registry

                registry = get_agent_registry()
                agent_info = await registry.get_agent(agent_id=agent_id)

                if not agent_info:
                    return f"❌ Agent not found: `{agent_id}`"

                registered_at = (
                    agent_info.registered_at.strftime("%Y-%m-%d %H:%M")
                    if agent_info.registered_at
                    else "unknown"
                )
                last_seen = (
                    agent_info.last_seen.strftime("%Y-%m-%d %H:%M")
                    if agent_info.last_seen
                    else "unknown"
                )
                lines = [
                    f"🤖 **{agent_info.name}**\n",
                    f"**Agent ID:** `{agent_info.agent_id}`",
                    f"**URL:** {agent_info.url}",
                    f"**Status:** {agent_info.status.value if hasattr(agent_info.status, 'value') else agent_info.status}",
                    f"**Version:** {agent_info.version}",
                    f"**Registered:** {registered_at}",
                    f"**Last Seen:** {last_seen}",
                    f"\n**Capabilities ({len(agent_info.capabilities)}):**\n",
                ]
                for cap in agent_info.capabilities:
                    lines.append(f"• `{cap.name}` - {cap.description}")
                    if cap.tags:
                        lines.append(f"  Tags: {', '.join(cap.tags)}")
                return "\n".join(lines)

            elif args[0] == "status":
                import httpx

                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get("http://localhost:8000/health/a2a")
                        if response.status_code == 200:
                            data = response.json()
                            caps_display = "\n".join(
                                f"• `{c}`" for c in data.get("capabilities", [])[:10]
                            )
                            return (
                                f"🤖 **A2A System Status**\n\n"
                                f"**Enabled:** {'✅ Yes' if data.get('enabled') else '❌ No'}\n"
                                f"**Registered Agents:** {data.get('registered_agents', 0)}\n"
                                f"**Online Agents:** {data.get('online_agents', 0)}\n"
                                f"**Available Capabilities:** {len(data.get('capabilities', []))}\n"
                                f"**Recent Delegations (24h):** {data.get('recent_delegations_24h', 0)}\n\n"
                                f"**Capabilities:**\n{caps_display}"
                            )
                        else:
                            return f"❌ Failed to get A2A status: HTTP {response.status_code}"
                except Exception as status_err:
                    return f"❌ Failed to get A2A status: {status_err}"
            else:
                return "❌ Unknown A2A command. Try `/a2a help` for available commands."

        except Exception as e:
            logger.error("a2a_command_error", error=str(e))
            return f"❌ Error: {e}"

    async def _process_message(self, message: ChannelMessage) -> None:
        """Process regular message and generate AI response."""
        try:
            async with get_db_session() as db_session:
                # Initialize managers
                redis_cache = RedisCache(get_redis())
                session_mgr = SessionManager(redis_cache, db_session)
                context_builder = ContextBuilder(db_session)
                model_selector = ModelSelector(db_session)

                # Get or create session
                session_data = await session_mgr.get_or_create_session(
                    message.channel_type, message.user_id, message.username
                )

                conversation_id = uuid.UUID(session_data.conversation_id)
                user_id = uuid.UUID(session_data.user_id)

                # Add user message to database
                await context_builder.add_user_message(conversation_id, message.content)

                # Build conversation context
                system_prompt = PromptManager.get_system_prompt(message.channel_type)

                context = await context_builder.build_context(
                    conversation_id, system_prompt=system_prompt
                )

                # Select model
                model = await model_selector.select_model(
                    user_id, conversation_id, message.channel_type
                )

                # Generate AI response
                logger.info(
                    "generating_ai_response",
                    model=model,
                    conversation_id=str(conversation_id),
                )

                response_content, token_count = await self.ai_client.generate_response(
                    messages=context, model=model
                )

                # Save assistant message
                await context_builder.add_assistant_message(
                    conversation_id,
                    response_content,
                    model_used=model,
                    token_count=token_count,
                )

                # Update session activity
                await session_mgr.update_session_activity(message.channel_type, message.user_id)
                await session_mgr.increment_message_count(message.channel_type, message.user_id)

                # Send response through channel
                await self.router.send_message(
                    message.channel_type, message.user_id, response_content
                )

                logger.info(
                    "message_processed_successfully",
                    conversation_id=str(conversation_id),
                    model=model,
                    tokens=token_count,
                )

        except Exception as e:
            logger.error(
                "message_processing_failed",
                error=str(e),
                error_type=type(e).__name__,
                channel_type=message.channel_type,
                user_id=message.user_id,
            )

            # Send error message to user
            error_message = (
                "Sorry, I encountered an error processing your message. Please try again."
            )
            await self.router.send_message(message.channel_type, message.user_id, error_message)
