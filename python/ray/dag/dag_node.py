import copy
from ray.experimental.channel.auto_transport_type import AutoTransportType
from ray.experimental.channel.torch_tensor_type import TorchTensorType
import ray
from ray.dag.base import DAGNodeBase
from ray.dag.py_obj_scanner import _PyObjScanner
from ray.util.annotations import DeveloperAPI

from itertools import chain

from typing import (
    Optional,
    Union,
    List,
    Tuple,
    Dict,
    Any,
    TypeVar,
    Callable,
    Literal,
)
import uuid
import asyncio

from ray.dag.compiled_dag_node import build_compiled_dag_from_ray_dag
from ray.experimental.channel import ChannelOutputType
from ray.experimental.channel.communicator import Communicator
from ray.experimental.util.types import Device

T = TypeVar("T")


@DeveloperAPI
class DAGNode(DAGNodeBase):
    """Abstract class for a node in a Ray task graph.

    A node has a type (e.g., FunctionNode), data (e.g., function options and
    body), arguments (Python values, DAGNodes, and DAGNodes nested within Python
    argument values) and options (Ray API .options() used for function, class
    or class method)
    """

    def __init__(
        self,
        args: Tuple[Any],
        kwargs: Dict[str, Any],
        options: Dict[str, Any],
        other_args_to_resolve: Dict[str, Any],
    ):
        """
        args:
            args (Tuple[Any]): Bound node arguments.
                ex: func_or_class.bind(1)
            kwargs (Dict[str, Any]): Bound node keyword arguments.
                ex: func_or_class.bind(a=1)
            options (Dict[str, Any]): Bound node options arguments.
                ex: func_or_class.options(num_cpus=2)
            other_args_to_resolve (Dict[str, Any]): Bound kwargs to resolve
                that's specific to subclass implementation without exposing
                as args in base class, example: ClassMethodNode
        """
        self._bound_args: Tuple[Any] = args or []
        self._bound_kwargs: Dict[str, Any] = kwargs or {}
        self._bound_options: Dict[str, Any] = options or {}
        self._bound_other_args_to_resolve: Optional[Dict[str, Any]] = (
            other_args_to_resolve or {}
        )

        # The list of nodes that use this DAG node as an argument.
        self._downstream_nodes: List["DAGNode"] = []

        # UUID that is not changed over copies of this node.
        self._stable_uuid = uuid.uuid4().hex

        # Indicates whether this DAG node contains nested DAG nodes.
        # Nested DAG nodes are allowed in traditional DAGs but not
        # in Ray Compiled Graphs, except for MultiOutputNode.
        self._args_contain_nested_dag_node = False

        # The list of nodes that this DAG node uses as an argument.
        self._upstream_nodes: List["DAGNode"] = self._collect_upstream_nodes()

        # Cached values from last call to execute()
        self.cache_from_last_execute = {}

        self._type_hint: ChannelOutputType = ChannelOutputType()

        # If the original type hint is an AutoTransportType, we make a copy
        # here when it is resolved to the actual type, as additional debugging
        # information. Otherwise, it is None.
        self._original_type_hint: Optional[ChannelOutputType] = None

        # Whether this node calls `experimental_compile`.
        self.is_cgraph_output_node = False

    def _collect_upstream_nodes(self) -> List["DAGNode"]:
        """
        Retrieve upstream nodes and update their downstream dependencies.

        Currently, the DAG assumes that all DAGNodes in `args`, `kwargs`, and
        `other_args_to_resolve` are upstream nodes. However, Ray Compiled Graphs
        builds the upstream/downstream relationship based only on args. Be cautious
        when persisting DAGNodes in `other_args_to_resolve` and kwargs in the future.

        TODO (kevin85421): Currently, the upstream nodes and downstream nodes have
        circular references. Therefore, it relies on the garbage collector to clean
        them up instead of reference counting. We should consider using weak references
        to avoid circular references.
        """
        upstream_nodes: List["DAGNode"] = []

        # Ray Compiled Graphs do not allow nested DAG nodes in arguments.
        # Specifically, a DAGNode should not be placed inside any type of
        # container. However, we only know if this is a compiled graph
        # when calling `experimental_compile`. Therefore, we need to check
        # in advance if the arguments contain nested DAG nodes and raise
        # an error after compilation.
        assert hasattr(self._bound_args, "__iter__")
        for arg in self._bound_args:
            if isinstance(arg, DAGNode):
                upstream_nodes.append(arg)
            else:
                scanner = _PyObjScanner()
                dag_nodes = scanner.find_nodes(arg)
                upstream_nodes.extend(dag_nodes)
                scanner.clear()
                self._args_contain_nested_dag_node = len(dag_nodes) > 0

        scanner = _PyObjScanner()
        other_upstream_nodes: List["DAGNode"] = scanner.find_nodes(
            [
                self._bound_kwargs,
                self._bound_other_args_to_resolve,
            ]
        )
        upstream_nodes.extend(other_upstream_nodes)
        scanner.clear()
        # Update dependencies.
        for upstream_node in upstream_nodes:
            upstream_node._downstream_nodes.append(self)
        return upstream_nodes

    def with_tensor_transport(
        self,
        transport: Optional[Union[str, Communicator]] = "auto",
        device: Literal["default", "cpu", "gpu", "cuda"] = "default",
        _static_shape: bool = False,
        _direct_return: bool = False,
    ):
        """
        Configure the torch tensor transport for this node.

        Args:
            transport: Specifies the tensor transport mechanism.
                - "accelerator": Tensors are communicated using accelerator-specific backends
                (e.g., NCCL, XLA, or vendor-provided transport). This is the recommended option
                for most use cases, as it supports extensibility and future hardware backends.
                - "nccl": Tensors are passed explicitly via NCCL. This option is kept for
                backwards compatibility and may be removed in the future. Use "accelerator"
                instead unless you have legacy requirements.
                - "shm": Tensors are passed via host shared memory and gRPC. Typically used
                when accelerator-based transport is unavailable or not suitable.
                - "auto" (default): The system automatically selects the appropriate transport
                mechanism based on the sender and receiver, usually preferring accelerator-based
                transport when available.
            device: The target device to use for the tensor transport.
                "default": The tensor will maintain its original device placement from the sender
                "cpu": The tensor will be explicitly moved to CPU device in the receiver
                "gpu" or "cuda": The tensor will be explicitly moved to GPU device in the receiver
            _static_shape: A hint indicating whether the shape(s) and dtype(s)
                of tensor(s) contained in this value always remain the same
                across different executions of the DAG. If this is True, the
                transport will be more efficient.
            _direct_return: Whether the tensor is sent directly or inside of
                other data. If a "nccl" transport is used, this allows the
                sender and receiver to eliminate performance overhead from
                an additional data transfer.
        """
        try:
            device = Device(device)
        except ValueError:
            valid_devices = ", ".join(f"'{d.value}'" for d in Device)
            raise ValueError(
                f"Invalid device '{device}'. Valid options are: {valid_devices}."
            )
        if transport == "auto":
            self._type_hint = AutoTransportType(
                device=device,
                _static_shape=_static_shape,
                _direct_return=_direct_return,
            )
        elif transport == "nccl":
            self._type_hint = TorchTensorType(
                transport="accelerator",
                device=device,
                _static_shape=_static_shape,
                _direct_return=_direct_return,
            )
        elif transport == "accelerator":
            self._type_hint = TorchTensorType(
                transport == "accelerator",
                device=device,
                _static_shape=_static_shape,
                _direct_return=_direct_return,
            )
        elif transport == "shm":
            self._type_hint = TorchTensorType(
                device=device,
                _static_shape=_static_shape,
                _direct_return=_direct_return,
            )
        else:
            if not isinstance(transport, Communicator):
                raise ValueError(
                    "transport must be 'auto', 'nccl', 'shm', 'accelerator' or "
                    "a Communicator type"
                )
            self._type_hint = TorchTensorType(
                transport=transport,
                device=device,
                _static_shape=_static_shape,
                _direct_return=_direct_return,
            )
        return self

    @property
    def type_hint(self) -> ChannelOutputType:
        return self._type_hint

    @type_hint.setter
    def type_hint(self, type_hint: ChannelOutputType) -> None:
        if isinstance(self._type_hint, AutoTransportType):
            self._original_type_hint = self._type_hint
        self._type_hint = type_hint

    def get_args(self) -> Tuple[Any]:
        """Return the tuple of arguments for this node."""

        return self._bound_args

    def get_kwargs(self) -> Dict[str, Any]:
        """Return the dict of keyword arguments for this node."""

        return self._bound_kwargs.copy()

    def get_options(self) -> Dict[str, Any]:
        """Return the dict of options arguments for this node."""

        return self._bound_options.copy()

    def get_other_args_to_resolve(self) -> Dict[str, Any]:
        """Return the dict of other args to resolve arguments for this node."""
        return self._bound_other_args_to_resolve.copy()

    def get_stable_uuid(self) -> str:
        """Return stable uuid for this node.
        1) Generated only once at first instance creation
        2) Stable across pickling, replacement and JSON serialization.
        """
        return self._stable_uuid

    async def get_object_refs_from_last_execute(self) -> Dict[str, Any]:
        """Gets cached object refs from the last call to execute().

        After this DAG is executed through execute(), retrieves a map between node
        UUID to a reference to the return value of the default executor on that node.
        """
        cache = {}
        for node_uuid, value in self.cache_from_last_execute.items():
            if isinstance(value, asyncio.Task):
                cache[node_uuid] = await value
            else:
                cache[node_uuid] = value

        return cache

    def clear_cache(self):
        self.cache_from_last_execute = {}

    def experimental_compile(
        self,
        _submit_timeout: Optional[float] = None,
        _buffer_size_bytes: Optional[int] = None,
        enable_asyncio: bool = False,
        _max_inflight_executions: Optional[int] = None,
        _max_buffered_results: Optional[int] = None,
        _overlap_gpu_communication: Optional[bool] = None,
        _default_communicator: Optional[Union[Communicator, str]] = "create",
    ) -> "ray.dag.CompiledDAG":
        """Compile an accelerated execution path for this DAG.

        Args:
            _submit_timeout: The maximum time in seconds to wait for execute() calls.
                None means using default timeout, 0 means immediate timeout
                (immediate success or timeout without blocking), -1 means
                infinite timeout (block indefinitely).
            _buffer_size_bytes: The initial buffer size in bytes for messages
                that can be passed between tasks in the DAG. The buffers will
                be automatically resized if larger messages are written to the
                channel.
            enable_asyncio: Whether to enable asyncio for this DAG.
            _max_inflight_executions: The maximum number of in-flight executions that
                can be submitted via `execute` or `execute_async` before consuming
                the output using `ray.get()`. If the caller submits more executions,
                `RayCgraphCapacityExceeded` is raised.
            _max_buffered_results: The maximum number of results that can be
                buffered at the driver. If more than this number of results
                are buffered, `RayCgraphCapacityExceeded` is raised. Note that
                when result corresponding to an execution is retrieved
                (by calling `ray.get()` on a `CompiledDAGRef` or
                `CompiledDAGRef` or await on a `CompiledDAGFuture`), results
                corresponding to earlier executions that have not been retrieved
                yet are buffered.
            _overlap_gpu_communication: (experimental) Whether to overlap GPU
                communication with computation during DAG execution. If True, the
                communication and computation can be overlapped, which can improve
                the performance of the DAG execution. If None, the default value
                will be used.
            _default_communicator: The default communicator to use to transfer
                tensors. Three types of values are valid. (1) Communicator:
                For p2p operations, this is the default communicator
                to use for nodes annotated with `with_tensor_transport()` and when
                shared memory is not the desired option (e.g., when transport="nccl",
                or when transport="auto" for communication between two different GPUs).
                For collective operations, this is the default communicator to use
                when a custom communicator is not specified.
                (2) "create": for each collective operation without a custom communicator
                specified, a communicator is created and initialized on its involved actors,
                or an already created communicator is reused if the set of actors is the same.
                For all p2p operations without a custom communicator specified, it reuses
                an already created collective communicator if the p2p actors are a subset.
                Otherwise, a new communicator is created.
                (3) None: a ValueError will be thrown if a custom communicator is not specified.

        Returns:
            A compiled DAG.
        """
        from ray.dag import DAGContext

        ctx = DAGContext.get_current()
        if _buffer_size_bytes is None:
            _buffer_size_bytes = ctx.buffer_size_bytes

        # Validate whether this DAG node has already been compiled.
        if self.is_cgraph_output_node:
            raise ValueError(
                "It is not allowed to call `experimental_compile` on the same DAG "
                "object multiple times no matter whether `teardown` is called or not. "
                "Please reuse the existing compiled DAG or create a new one."
            )
        # Whether this node is an output node in the DAG. We cannot determine
        # this in the constructor because the output node is determined when
        # `experimental_compile` is called.
        self.is_cgraph_output_node = True
        return build_compiled_dag_from_ray_dag(
            self,
            _submit_timeout,
            _buffer_size_bytes,
            enable_asyncio,
            _max_inflight_executions,
            _max_buffered_results,
            _overlap_gpu_communication,
            _default_communicator,
        )

    def execute(
        self, *args, _ray_cache_refs: bool = False, **kwargs
    ) -> Union[ray.ObjectRef, "ray.actor.ActorHandle"]:
        """Execute this DAG using the Ray default executor _execute_impl().

        Args:
            _ray_cache_refs: If true, stores the default executor's return values
                on each node in this DAG in a cache. These should be a mix of:
                - ray.ObjectRefs pointing to the outputs of method and function nodes
                - Serve handles for class nodes
                - resolved values representing user input at runtime
        """

        def executor(node):
            return node._execute_impl(*args, **kwargs)

        result = self.apply_recursive(executor)
        if _ray_cache_refs:
            self.cache_from_last_execute = executor.cache
        return result

    def _get_toplevel_child_nodes(self) -> List["DAGNode"]:
        """Return the list of nodes specified as top-level args.

        For example, in `f.remote(a, [b])`, only `a` is a top-level arg.

        This list of nodes are those that are typically resolved prior to
        task execution in Ray. This does not include nodes nested within args.
        For that, use ``_get_all_child_nodes()``.
        """

        # we use List instead of Set here because the hash key of the node
        # object changes each time we create it. So if using Set here, the
        # order of returned children can be different if we create the same
        # nodes and dag one more time.
        children = []
        for a in self.get_args():
            if isinstance(a, DAGNode):
                if a not in children:
                    children.append(a)
        for a in self.get_kwargs().values():
            if isinstance(a, DAGNode):
                if a not in children:
                    children.append(a)
        for a in self.get_other_args_to_resolve().values():
            if isinstance(a, DAGNode):
                if a not in children:
                    children.append(a)
        return children

    def _get_all_child_nodes(self) -> List["DAGNode"]:
        """Return the list of nodes referenced by the args, kwargs, and
        args_to_resolve in current node, even they're deeply nested.

        Examples:
            f.remote(a, [b]) -> [a, b]
            f.remote(a, [b], key={"nested": [c]}) -> [a, b, c]
        """

        scanner = _PyObjScanner()
        # we use List instead of Set here, reason explained
        # in `_get_toplevel_child_nodes`.
        children = []
        for n in scanner.find_nodes(
            [
                self._bound_args,
                self._bound_kwargs,
                self._bound_other_args_to_resolve,
            ]
        ):
            if n not in children:
                children.append(n)
        scanner.clear()
        return children

    def _apply_and_replace_all_child_nodes(
        self, fn: "Callable[[DAGNode], T]"
    ) -> "DAGNode":
        """Apply and replace all immediate child nodes using a given function.

        This is a shallow replacement only. To recursively transform nodes in
        the DAG, use ``apply_recursive()``.

        Args:
            fn: Callable that will be applied once to each child of this node.

        Returns:
            New DAGNode after replacing all child nodes.
        """

        replace_table = {}
        # CloudPickler scanner object for current layer of DAGNode. Same
        # scanner should be use for a full find & replace cycle.
        scanner = _PyObjScanner()
        # Find all first-level nested DAGNode children in args.
        # Update replacement table and execute the replace.
        for node in scanner.find_nodes(
            [
                self._bound_args,
                self._bound_kwargs,
                self._bound_other_args_to_resolve,
            ]
        ):
            if node not in replace_table:
                replace_table[node] = fn(node)
        new_args, new_kwargs, new_other_args_to_resolve = scanner.replace_nodes(
            replace_table
        )
        scanner.clear()

        # Return updated copy of self.
        return self._copy(
            new_args, new_kwargs, self.get_options(), new_other_args_to_resolve
        )

    def apply_recursive(self, fn: "Callable[[DAGNode], T]") -> T:
        """Apply callable on each node in this DAG in a bottom-up tree walk.

        Args:
            fn: Callable that will be applied once to each node in the
                DAG. It will be applied recursively bottom-up, so nodes can
                assume the fn has been applied to their args already.

        Returns:
            Return type of the fn after application to the tree.
        """

        if not type(fn).__name__ == "_CachingFn":

            class _CachingFn:
                def __init__(self, fn):
                    self.cache = {}
                    self.fn = fn
                    self.fn.cache = self.cache
                    self.input_node_uuid = None

                def __call__(self, node: "DAGNode"):
                    from ray.dag.input_node import InputNode

                    if node._stable_uuid not in self.cache:
                        self.cache[node._stable_uuid] = self.fn(node)
                    if isinstance(node, InputNode):
                        if not self.input_node_uuid:
                            self.input_node_uuid = node._stable_uuid
                        elif self.input_node_uuid != node._stable_uuid:
                            raise AssertionError(
                                "Each DAG should only have one unique InputNode."
                            )
                    return self.cache[node._stable_uuid]

            fn = _CachingFn(fn)
        else:
            if self._stable_uuid in fn.cache:
                return fn.cache[self._stable_uuid]

        return fn(
            self._apply_and_replace_all_child_nodes(
                lambda node: node.apply_recursive(fn)
            )
        )

    def traverse_and_apply(self, fn: "Callable[[DAGNode], T]"):
        """
        Traverse all nodes in the connected component of the DAG that contains
        the `self` node, and apply the given function to each node.
        """
        visited = set()
        queue = [self]
        cgraph_output_node: Optional[DAGNode] = None

        while queue:
            node = queue.pop(0)
            if node._args_contain_nested_dag_node:
                self._raise_nested_dag_node_error(node._bound_args)

            if node not in visited:
                if node.is_cgraph_output_node:
                    # Validate whether there are multiple nodes that call
                    # `experimental_compile`.
                    if cgraph_output_node is not None:
                        raise ValueError(
                            "The DAG was compiled more than once. The following two "
                            "nodes call `experimental_compile`: "
                            f"(1) {cgraph_output_node}, (2) {node}"
                        )
                    cgraph_output_node = node
                fn(node)
                visited.add(node)
                """
                Add all unseen downstream and upstream nodes to the queue.
                This function should be called by the root of the DAG. However,
                in some invalid cases, some nodes may not be descendants of the
                root. Therefore, we also add upstream nodes to the queue so that
                a meaningful error message can be raised when the DAG is compiled.

                ```
                with InputNode() as inp:
                    dag = MultiOutputNode([a1.inc.bind(inp), a2.inc.bind(1)])
                ```

                In the above example, `a2.inc` is not a descendant of inp. If we only
                add downstream nodes to the queue, the `a2.inc` node will not be visited
                , and the error message will be hard to understand, such as a key error
                in the compiled DAG.
                """
                for neighbor in chain.from_iterable(
                    [node._downstream_nodes, node._upstream_nodes]
                ):
                    if neighbor not in visited:
                        queue.append(neighbor)

    def _raise_nested_dag_node_error(self, args):
        """
        Raise an error for nested DAGNodes in Ray Compiled Graphs.

        Args:
            args: The arguments of the DAGNode.
        """
        for arg in args:
            if isinstance(arg, DAGNode):
                continue
            else:
                scanner = _PyObjScanner()
                dag_nodes = scanner.find_nodes([arg])
                scanner.clear()
                if len(dag_nodes) > 0:
                    raise ValueError(
                        f"Found {len(dag_nodes)} DAGNodes from the arg {arg} "
                        f"in {self}. Please ensure that the argument is a "
                        "single DAGNode and that a DAGNode is not allowed to "
                        "be placed inside any type of container."
                    )
        raise AssertionError(
            "A DAGNode's args should contain nested DAGNodes as args, "
            "but none were found during the compilation process. This is a "
            "Ray internal error. Please report this issue to the Ray team."
        )

    def _find_root(self) -> "DAGNode":
        """
        Return the root node of the DAG. The root node must be an InputNode.
        """
        from ray.dag.input_node import InputNode

        node = self
        while not isinstance(node, InputNode):
            if len(node._upstream_nodes) == 0:
                raise ValueError(
                    "No InputNode found in the DAG: when traversing upwards, "
                    f"no upstream node was found for {node}."
                )
            node = node._upstream_nodes[0]
        return node

    def apply_functional(
        self,
        source_input_list: Any,
        predicate_fn: Callable,
        apply_fn: Callable,
    ):
        """
        Apply a given function to DAGNodes in source_input_list, and return
        the replaced inputs without mutating or coping any DAGNode.

        Args:
            source_input_list: Source inputs to extract and apply function on
                all children DAGNode instances.
            predicate_fn: Applied on each DAGNode instance found and determine
                if we should apply function to it. Can be used to filter node
                types.
            apply_fn: Function to apply on the node on bound attributes. Example::

                apply_fn = lambda node: node._get_serve_deployment_handle(
                    node._deployment, node._bound_other_args_to_resolve
                )

        Returns:
            replaced_inputs: Outputs of apply_fn on DAGNodes in
                source_input_list that passes predicate_fn.
        """
        replace_table = {}
        scanner = _PyObjScanner()
        for node in scanner.find_nodes(source_input_list):
            if predicate_fn(node) and node not in replace_table:
                replace_table[node] = apply_fn(node)

        replaced_inputs = scanner.replace_nodes(replace_table)
        scanner.clear()

        return replaced_inputs

    def _execute_impl(
        self, *args, **kwargs
    ) -> Union[ray.ObjectRef, "ray.actor.ActorHandle"]:
        """Execute this node, assuming args have been transformed already."""
        raise NotImplementedError

    def _copy_impl(
        self,
        new_args: List[Any],
        new_kwargs: Dict[str, Any],
        new_options: Dict[str, Any],
        new_other_args_to_resolve: Dict[str, Any],
    ) -> "DAGNode":
        """Return a copy of this node with the given new args."""
        raise NotImplementedError

    def _copy(
        self,
        new_args: List[Any],
        new_kwargs: Dict[str, Any],
        new_options: Dict[str, Any],
        new_other_args_to_resolve: Dict[str, Any],
    ) -> "DAGNode":
        """Return a copy of this node with the given new args."""
        instance = self._copy_impl(
            new_args, new_kwargs, new_options, new_other_args_to_resolve
        )
        instance._stable_uuid = self._stable_uuid
        instance._type_hint = copy.deepcopy(self._type_hint)
        instance._original_type_hint = copy.deepcopy(self._original_type_hint)
        return instance

    def __getstate__(self):
        """Required due to overriding `__getattr__` else pickling fails."""
        return self.__dict__

    def __setstate__(self, d: Dict[str, Any]):
        """Required due to overriding `__getattr__` else pickling fails."""
        self.__dict__.update(d)

    def __getattr__(self, attr: str):
        if attr == "bind":
            raise AttributeError(f".bind() cannot be used again on {type(self)} ")
        elif attr == "remote":
            raise AttributeError(
                f".remote() cannot be used on {type(self)}. To execute the task "
                "graph for this node, use .execute()."
            )
        else:
            return self.__getattribute__(attr)
