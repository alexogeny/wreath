from __future__ import annotations

import wreath.agents as agents


def test_public_agent_api_is_lazy_and_vendor_neutral() -> None:
    expected = {
        "AgentArtifactManager",
        "AgentCatalog",
        "AgentInvocationContext",
        "AgentProfile",
        "AgentRuntime",
        "AgentObservability",
        "AnthropicMessagesBackplane",
        "ApprovalStore",
        "AzureOpenAIBackplane",
        "BackplaneError",
        "ChatApprovalFlow",
        "ContextAssembler",
        "DurableAgent",
        "EffectCheckpointStore",
        "FederatedToolCatalog",
        "HTTPClientTransport",
        "InMemoryApprovalStore",
        "MCPToolCatalog",
        "MCPRemoteScopeError",
        "MCPHTTPClientTransport",
        "RemoteMCPClient",
        "RemoteMCPToolCatalog",
        "RoutedBackplane",
        "MemoryStore",
        "OpenAIResponsesBackplane",
        "ModelBackplane",
        "ModelCandidate",
        "ModelMessage",
        "ModelRequest",
        "ModelResponseEvent",
        "ModelRoutePolicy",
        "ModelUsage",
        "ToolSpecification",
        "stable_tool_call_id",
        "stable_turn_id",
    }

    assert expected <= set(agents.__all__)
    assert expected <= set(dir(agents))
    assert "openai" not in agents.ModelBackplane.__module__.lower()
    assert "anthropic" not in agents.ModelBackplane.__module__.lower()


def test_every_declared_agent_export_resolves_lazily() -> None:
    assert set(agents.__all__) == set(agents._EXPORTS)
    assert all(getattr(agents, name) is not None for name in agents.__all__)
