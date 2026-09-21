"""Protobuf schema for the Devin/Cognition Cascade chat API.

Field numbers mirror exa.api_server_pb.GetChatMessageRequest/Response as sent
by the Devin CLI. Message classes are built dynamically on the default
descriptor pool — no codegen step required.
"""
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf import timestamp_pb2  # noqa: F401  (registers Timestamp)

_POOL = descriptor_pool.Default()

T = descriptor_pb2.FieldDescriptorProto


def _f(name, num, ftype, label=T.LABEL_OPTIONAL, type_name=None):
    f = T(name=name, number=num, label=label, type=ftype)
    if type_name:
        f.type_name = type_name
    return f


def _msg(name, fields):
    return descriptor_pb2.DescriptorProto(name=name, field=fields)


STR, I32, U64, DBL, FLT, MSG = (
    T.TYPE_STRING, T.TYPE_INT32, T.TYPE_UINT64,
    T.TYPE_DOUBLE, T.TYPE_FLOAT, T.TYPE_MESSAGE,
)
REP = T.LABEL_REPEATED

_fd = descriptor_pb2.FileDescriptorProto(
    name="devin.proto", package="devin", syntax="proto3",
    dependency=["google/protobuf/timestamp.proto"],
    message_type=[
        _msg("Metadata", [
            _f("ide_name", 1, STR), _f("ide_version", 2, STR),
            _f("api_key", 3, STR), _f("locale", 4, STR), _f("os", 5, STR),
            _f("extension_version", 7, STR), _f("request_id", 9, U64),
            _f("session_id", 10, STR), _f("ide_name_2", 12, STR),
            _f("timestamp", 16, MSG, type_name=".google.protobuf.Timestamp"),
            _f("user_jwt", 21, STR), _f("trigger_id", 25, STR),
            _f("ide_name_3", 26, STR), _f("ide_name_4", 28, STR),
        ]),
        _msg("ChatToolCall", [
            _f("id", 1, STR), _f("name", 2, STR), _f("arguments", 3, STR),
        ]),
        _msg("ImageData", [
            _f("base64_data", 1, STR), _f("mime_type", 2, STR),
        ]),
        _msg("ChatMessagePrompt", [
            _f("source", 2, I32), _f("prompt", 3, STR),
            _f("num_tokens", 4, I32), _f("is_user_input", 5, I32),
            _f("tool_calls", 6, MSG, REP, ".devin.ChatToolCall"),
            _f("tool_call_id", 7, STR),
            _f("images", 10, MSG, REP, ".devin.ImageData"),
            _f("thinking", 11, STR), _f("thinking_signature", 12, STR),
            _f("thinking_redacted", 13, I32), _f("signature_type", 18, STR),
        ]),
        _msg("CompletionConfiguration", [
            _f("num_completions", 1, I32), _f("max_tokens", 2, I32),
            _f("max_newlines", 3, I32), _f("temperature", 5, DBL),
            _f("top_k", 7, I32), _f("top_p", 8, DBL),
        ]),
        _msg("TrajectoryReference", [
            _f("trajectory_id", 1, STR), _f("f3", 3, I32), _f("f4", 4, I32),
        ]),
        _msg("ChatToolDefinition", [
            _f("name", 1, STR), _f("description", 2, STR),
            _f("parameters_json", 3, STR),
        ]),
        _msg("UsageValue", [_f("v", 2, FLT)]),
        _msg("UsageMetric", [
            _f("value", 4, MSG, type_name=".devin.UsageValue"),
            _f("metric", 5, STR),
        ]),
        _msg("UsageReport", [
            _f("metrics", 2, MSG, REP, ".devin.UsageMetric"),
        ]),
        _msg("GetChatMessageRequest", [
            _f("metadata", 1, MSG, type_name=".devin.Metadata"),
            _f("prompt", 2, STR),
            _f("chat_message_prompts", 3, MSG, REP, ".devin.ChatMessagePrompt"),
            _f("request_type", 7, I32),
            _f("completion_config", 8, MSG, type_name=".devin.CompletionConfiguration"),
            _f("tools", 10, MSG, REP, ".devin.ChatToolDefinition"),
            _f("trajectory_ref", 15, MSG, type_name=".devin.TrajectoryReference"),
            _f("cascade_id", 16, STR),
            _f("planner_mode", 20, I32),
            _f("chat_model_uid", 21, STR),
        ]),
        _msg("GetChatMessageResponse", [
            _f("message_id", 1, STR), _f("delta_text", 3, STR),
            _f("stop_reason", 5, I32),
            _f("delta_tool_calls", 6, MSG, REP, ".devin.ChatToolCall"),
            _f("delta_thinking", 9, STR), _f("thinking_signature", 10, STR),
            _f("thinking_redacted", 11, I32), _f("signature_type", 21, STR),
            _f("usage", 28, MSG, type_name=".devin.UsageReport"),
        ]),
        _msg("GetUserJwtRequest", [
            _f("metadata", 1, MSG, type_name=".devin.Metadata"),
        ]),
        _msg("GetUserJwtResponse", [_f("jwt", 1, STR)]),
    ],
)

try:
    _POOL.Add(_fd)
except Exception:
    pass  # already registered on re-import


def cls(name):
    return message_factory.GetMessageClass(_POOL.FindMessageTypeByName(f"devin.{name}"))


Metadata = cls("Metadata")
ChatToolCall = cls("ChatToolCall")
ImageData = cls("ImageData")
ChatMessagePrompt = cls("ChatMessagePrompt")
CompletionConfiguration = cls("CompletionConfiguration")
TrajectoryReference = cls("TrajectoryReference")
ChatToolDefinition = cls("ChatToolDefinition")
GetChatMessageRequest = cls("GetChatMessageRequest")
GetChatMessageResponse = cls("GetChatMessageResponse")
GetUserJwtRequest = cls("GetUserJwtRequest")
GetUserJwtResponse = cls("GetUserJwtResponse")
