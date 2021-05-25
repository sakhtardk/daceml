import copy
import ctypes
import itertools
from typing import List, Optional, Tuple, Dict, Union

import dace
from dace.frontend.common import einsum
from dace.registry import autoregister_params
from dace import nodes as nd, dtypes
import dace.transformation.transformation as xf

import daceml.onnx as donnx
from daceml.onnx.converters import clean_onnx_name
from daceml.onnx.op_implementations import pure_implementations, cudnn_implementations, CudnnBatchNormalizationTraining
import daceml.autodiff.utils as butils
from daceml.autodiff.base_abc import BackwardImplementation, BackwardContext, BackwardResult
from daceml.util import utils


def reverse_einsum_wrt_input(forward_node: donnx.ONNXEinsum,
                             input_name: str) -> Tuple[List[str], str]:
    """ Produce the einsum string that computes the grad of ``forward_node`` w.r.t. ``input_name``.

       :Note:
            There is an edge case we currently don't handle (can be implemented though). Something like ``'ii->i'``
            would become ``'i->ii'``. This is invalid because ``i`` is repeated in the output.

        :param forward_node: the einsum node to reverse.
        :param input_name: the connector on the forward node the produce the gradient computation for.
        :return: the list of forward node connectors required as inputs, and the einsum string. The first parameter of
                 the produced einsum string is implicitly the grad of ``Output``.
    """

    _, input_idx = donnx.parse_variadic_param(input_name)
    parser = einsum.EinsumParser(forward_node.equation)

    backward_input_expressions = [
        parser.output
    ] + parser.inputs[:input_idx] + parser.inputs[input_idx + 1:]
    backward_input_arrays = [
        f"Inputs__{i}" for i in itertools.chain(
            range(input_idx), range(input_idx + 1, len(parser.inputs)))
    ]

    einsum_str = f"{','.join(backward_input_expressions)}->{parser.inputs[input_idx]}"
    return backward_input_arrays, einsum_str


@autoregister_params(op="Einsum", name="default")
class DefaultEinsumBackward(BackwardImplementation):
    """ The symbolic autodiff can automatically derive matmuls, but the produced maps are more difficult to optimize.
    """
    @staticmethod
    def backward_can_be_applied(node: nd.Node, state: dace.SDFGState,
                                sdfg: dace.SDFG) -> bool:
        return pure_implementations.PureEinsum.forward_can_be_applied(
            node, state, sdfg)

    @staticmethod
    def backward(
        forward_node: nd.Node, context: BackwardContext,
        given_gradients: List[Optional[str]],
        required_gradients: List[Optional[str]]
    ) -> Tuple[nd.Node, BackwardResult]:

        nsdfg = dace.SDFG(forward_node.label + "_backward")
        nstate = nsdfg.add_state()

        # setup arrays
        output_desc = butils.forward_out_desc_with_name(
            forward_node, context, "Output")
        result = BackwardResult.empty()
        result.given_grad_names["Output"] = butils.add_backward_desc(
            nsdfg, context.forward_sdfg, output_desc, "Output")
        access_output_grad = nstate.add_read(result.given_grad_names["Output"])

        def create_access_node(connector: str) -> nd.AccessNode:
            nsdfg.add_datadesc(
                connector,
                butils.forward_in_desc_with_name(forward_node, context,
                                                 connector))
            return nstate.add_read(connector)

        # the forward inputs we will require
        # maps the connector name to the accessnode
        required_forward_inputs: Dict[str, nd.AccessNode] = {}

        for input_name in required_gradients:
            # we add an einsum for each required gradient
            forward_inputs, einsum_str = reverse_einsum_wrt_input(
                forward_node, input_name)

            einsum_node = donnx.ONNXEinsum(input_name + "_backward",
                                           equation=einsum_str)
            nstate.add_node(einsum_node)

            # the first input is always the output grad
            einsum_node.add_in_connector(f"Inputs__0")
            nstate.add_edge(
                access_output_grad, None, einsum_node, "Inputs__0",
                nsdfg.make_array_memlet(result.given_grad_names["Output"]))

            # add the other inputs from forward that we need
            for i, forward_input in enumerate(forward_inputs):
                connector = f"Inputs__{i + 1}"
                einsum_node.add_in_connector(connector)
                if forward_input not in required_forward_inputs:
                    required_forward_inputs[
                        forward_input] = create_access_node(forward_input)

                nstate.add_edge(required_forward_inputs[forward_input], None,
                                einsum_node, connector,
                                nsdfg.make_array_memlet(forward_input))

            # write out the gradient
            butils.forward_in_desc_with_name(forward_node, context, input_name)
            result.required_grad_names[
                input_name] = butils.add_backward_desc_for_connector(
                    nsdfg, forward_node, context, input_name, True)
            memlet = nsdfg.make_array_memlet(
                result.required_grad_names[input_name])
            nstate.add_edge(
                einsum_node, "Output",
                nstate.add_write(result.required_grad_names[input_name]), None,
                memlet)

        result_node = context.backward_state.add_nested_sdfg(
            nsdfg, None,
            set(result.given_grad_names.values()).union(
                required_forward_inputs),
            set(result.required_grad_names.values()))

        return result_node, result


@autoregister_params(op="Softmax", name="default")
class DefaultSoftmaxBackward(BackwardImplementation):
    @staticmethod
    def backward(
        forward_node: nd.Node, context: BackwardContext,
        given_gradients: List[Optional[str]],
        required_gradients: List[Optional[str]]
    ) -> Tuple[Union[nd.Node, dace.SDFG], BackwardResult]:

        dim = forward_node.axis

        output_shape = butils.forward_out_desc_with_name(
            forward_node, context, "output").shape
        output_dtype = butils.forward_out_desc_with_name(
            forward_node, context, "output").dtype

        sums_shape = list(copy.deepcopy(output_shape))
        sums_shape[dim] = 1

        def softmax_backward(output, output_grad, input_grad):
            prod = dace.define_local(output_shape, output_dtype)
            sums = dace.define_local(sums_shape, output_dtype)
            donnx.ONNXMul(A=output, B=output_grad, C=prod)
            donnx.ONNXReduceSum(data=prod,
                                reduced=sums,
                                keepdims=1,
                                axes=[dim])

            donnx.ONNXMul(A=output, B=sums, C=input_grad)
            # let's not use ONNXSub here; not sure how this inplace op is handled by ORT...
            input_grad[:] = prod - input_grad

        result_node, result = butils.backward_program_for_node(
            softmax_backward, context, forward_node)

        butils.connect_output_from_forward(forward_node, result_node, context,
                                           "output")

        return result_node, result


@autoregister_params(op="LogSoftmax", name="default")
class DefaultLogSoftmaxBackward(BackwardImplementation):
    @staticmethod
    def backward(
        forward_node: nd.Node, context: BackwardContext,
        given_gradients: List[Optional[str]],
        required_gradients: List[Optional[str]]
    ) -> Tuple[nd.Node, BackwardResult]:

        dim = forward_node.axis
        output_shape = butils.forward_out_desc_with_name(
            forward_node, context, "output").shape
        output_dtype = butils.forward_out_desc_with_name(
            forward_node, context, "output").dtype

        sums_shape = list(copy.deepcopy(output_shape))
        sums_shape[dim] = 1

        def logsoftmax_backward(output, output_grad, input_grad):
            exp_output = dace.define_local(output_shape, output_dtype)
            donnx.ONNXExp(input=output, output=exp_output)

            grad_output_sum = dace.define_local(sums_shape, output_dtype)
            donnx.ONNXReduceSum(data=output_grad,
                                reduced=grad_output_sum,
                                keepdims=1,
                                axes=[dim])
            # let's not use ONNXMul here; not sure how this inplace op is handled by ORT...
            exp_output[:] = exp_output * grad_output_sum
            donnx.ONNXSub(A=output_grad, B=exp_output, C=input_grad)

        result_node, result = butils.backward_program_for_node(
            logsoftmax_backward, context, forward_node)

        butils.connect_output_from_forward(forward_node, result_node, context,
                                           "output")
        return result_node, result


@autoregister_params(op="Conv", name="cuDNN")
class CuDNNConvBackward(BackwardImplementation):
    """ Conv backward using CUDNN.
        The algorithm implementations can be set using node._data_algorithm and node._filter_algorithm
        Available choices for data algorithm:
            "0"
            "1"
            "fft"
            "fft_tiling"
            "winograd"
            "winograd_nonfused"
        Available choices for filter algorithm:
            "0"
            "1"
            "fft"
            "fft_tiling"
            "3"
            "winograd_nonfused"
    """
    default_data_algorithm = "0"
    default_filter_algorithm = "0"

    @staticmethod
    def backward_can_be_applied(node: nd.Node, state: dace.SDFGState,
                                sdfg: dace.SDFG) -> bool:
        return cudnn_implementations.CudnnConvolution.forward_can_be_applied(
            node, state, sdfg)

    @staticmethod
    def backward(
        forward_node: nd.Node, context: BackwardContext,
        given_gradients: List[Optional[str]],
        required_gradients: List[Optional[str]]
    ) -> Tuple[nd.Node, BackwardResult]:

        nsdfg = dace.SDFG(forward_node.label + "_backward")
        X_desc = butils.forward_in_desc_with_name(forward_node, context, "X")

        T = X_desc.dtype

        # setup gradient arrays
        result = BackwardResult.empty()
        required_grads = set(required_gradients)
        for r in required_grads:
            result.required_grad_names[
                r] = butils.add_backward_desc_for_connector(nsdfg,
                                                            forward_node,
                                                            context,
                                                            r,
                                                            input=True)
        result.given_grad_names["Y"] = butils.add_backward_desc_for_connector(
            nsdfg, forward_node, context, "Y", input=False)

        # setup non-gradient arrays
        required_forward_inputs = ["W", "X"]
        for i in required_forward_inputs:
            new_desc = copy.deepcopy(
                butils.forward_in_desc_with_name(forward_node, context, i))
            new_desc.transient = False
            nsdfg.add_datadesc(i, new_desc)

        # setup state
        nstate = nsdfg.add_state()
        unique_id = "{}_{}_{}_{}_bwd".format(
            clean_onnx_name(forward_node.name), context.forward_sdfg.sdfg_id,
            context.forward_sdfg.node_id(context.forward_state),
            context.forward_state.node_id(forward_node))

        init_code = ""
        finalize_code = ""

        # add descriptor init code for gradients
        for r in required_grads:
            is_filter = r == "W"

            if r == "B":
                bias_desc = butils.forward_in_desc_with_name(
                    forward_node, context, "B")
                shape = [1, bias_desc.shape[0], 1, 1]
            else:
                shape = None

            init, exit = cudnn_implementations._cudnn_tensor_descriptor_code(
                nsdfg.arrays[result.required_grad_names[r]],
                f"{unique_id}_d{r}_desc",
                is_filter,
                shape=shape)
            init_code += init
            finalize_code += exit

        for r in required_forward_inputs:
            desc = butils.forward_in_desc_with_name(forward_node, context, r)
            is_filter = r == "W"
            init, exit = cudnn_implementations._cudnn_tensor_descriptor_code(
                desc, f"{unique_id}_{r}_desc", is_filter)
            init_code += init
            finalize_code += exit

        init, exit = cudnn_implementations._cudnn_tensor_descriptor_code(
            nsdfg.arrays[result.given_grad_names["Y"]], f"{unique_id}_dY_desc",
            False)
        init_code += init
        finalize_code += exit

        if hasattr(forward_node, "_data_algorithm"):
            data_algo = forward_node._data_algorithm
        else:
            data_algo = CuDNNConvBackward.default_data_algorithm

        if hasattr(forward_node, "_filter_algorithm"):
            filter_algo = forward_node._filter_algorithm
        else:
            filter_algo = CuDNNConvBackward.default_filter_algorithm

        # setup conv descriptor
        # we know from can_be_applied that the pads are symmetric
        pad_h, pad_w = forward_node.pads[0], forward_node.pads[1]
        stride_h, stride_w = forward_node.strides
        dilation_h, dilation_w = forward_node.dilations
        init_code += f"""
        __state->{unique_id}_conv_desc = new cudnnConvolutionDescriptor_t; 
        daceml::cudnn::CheckCudnnError(cudnnCreateConvolutionDescriptor(__state->{unique_id}_conv_desc));
        daceml::cudnn::CheckCudnnError(cudnnSetConvolution2dDescriptor(
            *__state->{unique_id}_conv_desc,
            {pad_h},
            {pad_w},
            {stride_h},
            {stride_w},
            {dilation_h},
            {dilation_w},
            CUDNN_CROSS_CORRELATION,
            {cudnn_implementations._DACE_DTYPE_TO_CUDNN_DTYPE[T]}));
        """
        finalize_code += f"""
        daceml::cudnn::CheckCudnnError(cudnnDestroyConvolutionDescriptor(*__state->{unique_id}_conv_desc));
        delete __state->{unique_id}_conv_desc;
        """

        # setup workspace
        init_code += \
            f"""
        {donnx.environments.cuDNN.handle_setup_code(forward_node, init_stream=False)}
        // Setup workspace for {unique_id}
        
        size_t data_ws_size;
        daceml::cudnn::CheckCudnnError(cudnnGetConvolutionBackwardDataWorkspaceSize(
            __dace_cudnn_handle,
            *__state->{unique_id}_W_desc,
            *__state->{unique_id}_dY_desc,
            *__state->{unique_id}_conv_desc,
            *__state->{unique_id}_dX_desc,
            CUDNN_CONVOLUTION_BWD_DATA_ALGO_{data_algo.upper()},
            &data_ws_size));
        size_t filter_ws_size;
        daceml::cudnn::CheckCudnnError(cudnnGetConvolutionBackwardFilterWorkspaceSize(
            __dace_cudnn_handle,
            *__state->{unique_id}_X_desc,
            *__state->{unique_id}_dY_desc,
            *__state->{unique_id}_conv_desc,
            *__state->{unique_id}_dW_desc,
            CUDNN_CONVOLUTION_BWD_FILTER_ALGO_{filter_algo.upper()},
            &filter_ws_size));
        
        size_t ws_size = max(filter_ws_size, data_ws_size);
        __state->{unique_id}_workspace_size = new size_t;
        *__state->{unique_id}_workspace_size = ws_size;
        cudaMalloc(&__state->{unique_id}_workspace, ws_size);
        """
        finalize_code += f"""
        cudaFree(__state->{unique_id}_workspace);
        delete __state->{unique_id}_workspace_size;
        """

        tasklet_code = f"""
        {donnx.environments.cuDNN.handle_setup_code(forward_node)}
        float alpha = 1.f;
        float beta = 0.f;
        daceml::cudnn::CheckCudnnError(cudnnConvolutionBackwardData(
            __dace_cudnn_handle,
            &alpha,
            *__state->{unique_id}_W_desc,
            _W,
            *__state->{unique_id}_dY_desc,
            _dY,
            *__state->{unique_id}_conv_desc,
            CUDNN_CONVOLUTION_BWD_DATA_ALGO_{data_algo.upper()},
            __state->{unique_id}_workspace,
            *__state->{unique_id}_workspace_size,
            &beta,
            *__state->{unique_id}_dX_desc,
            _dX));
        daceml::cudnn::CheckCudnnError(cudnnConvolutionBackwardFilter(
            __dace_cudnn_handle,
            &alpha,
            *__state->{unique_id}_X_desc,
            _X,
            *__state->{unique_id}_dY_desc,
            _dY,
            *__state->{unique_id}_conv_desc,
            CUDNN_CONVOLUTION_BWD_FILTER_ALGO_{filter_algo.upper()},
            __state->{unique_id}_workspace,
            *__state->{unique_id}_workspace_size,
            &beta,
            *__state->{unique_id}_dW_desc,
            _dW));
        """

        if "B" in required_gradients:
            tasklet_code += f"""
            daceml::cudnn::CheckCudnnError(cudnnConvolutionBackwardBias(
                __dace_cudnn_handle,
                &alpha,
                *__state->{unique_id}_dY_desc,
                _dY,
                &beta,
                *__state->{unique_id}_dB_desc,
                _dB));
            """

        init_code = "{\n" + init_code + "\n}"
        finalize_code = "{\n" + finalize_code + "\n}"
        tasklet = nstate.add_tasklet(
            unique_id, {
                f"_{i}": dace.pointer(T)
                for i in itertools.chain(["dY"], required_forward_inputs)
            }, {
                f"_d{i}": dace.pointer(T)
                for i in itertools.chain(required_gradients)
            },
            tasklet_code,
            dace.dtypes.Language.CPP,
            state_fields=[
                f"cudnnTensorDescriptor_t *{unique_id}_X_desc;",
                f"cudnnFilterDescriptor_t *{unique_id}_W_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dX_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dY_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dB_desc;",
                f"cudnnFilterDescriptor_t *{unique_id}_dW_desc;"
                f"cudnnConvolutionDescriptor_t *{unique_id}_conv_desc;",
                f"float *{unique_id}_workspace;",
                f"size_t *{unique_id}_workspace_size;"
            ],
            code_init=init_code,
            code_exit=finalize_code)
        tasklet.environments = {donnx.environments.cuDNN.full_class_path()}

        nstate.add_edge(
            nstate.add_read(result.given_grad_names["Y"]), None, tasklet,
            f"_dY", nsdfg.make_array_memlet((result.given_grad_names["Y"])))
        for name in required_forward_inputs:
            nstate.add_edge(nstate.add_read(name), None, tasklet, f"_{name}",
                            nsdfg.make_array_memlet(name))

        for name in required_gradients:
            arr_name = result.required_grad_names[name]
            nstate.add_edge(tasklet, f"_d{name}", nstate.add_write(arr_name),
                            None, nsdfg.make_array_memlet(arr_name))

        inputs = {result.given_grad_names["Y"]}.union(required_forward_inputs)
        outputs = {result.required_grad_names[n] for n in required_gradients}
        node = context.backward_state.add_nested_sdfg(nsdfg, None, inputs,
                                                      outputs)

        return node, result


@autoregister_params(op="BatchNormalization", name="cuDNN")
class CuDNNBatchNormBackward(BackwardImplementation):
    @staticmethod
    def backward_can_be_applied(node: nd.Node, state: dace.SDFGState,
                                sdfg: dace.SDFG) -> bool:
        return cudnn_implementations.CudnnBatchNormalizationTraining.forward_can_be_applied(
            node, state, sdfg)

    @staticmethod
    def backward(
        forward_node: nd.Node, context: BackwardContext,
        given_gradients: List[Optional[str]],
        required_gradients: List[Optional[str]]
    ) -> Tuple[nd.Node, BackwardResult]:

        nsdfg = dace.SDFG(forward_node.label + "_backward")
        X_desc = butils.forward_in_desc_with_name(forward_node, context, "X")
        scale_desc = butils.forward_in_desc_with_name(forward_node, context,
                                                      "scale")
        T = X_desc.dtype

        # setup arrays
        result = BackwardResult.empty()
        result.required_grad_names["X"] = butils.add_backward_desc(
            nsdfg, context.forward_sdfg, X_desc, "X")
        result.given_grad_names["Y"] = butils.add_backward_desc_for_connector(
            nsdfg, forward_node, context, "Y", input=False)
        result.required_grad_names[
            "scale"] = butils.add_backward_desc_for_connector(nsdfg,
                                                              forward_node,
                                                              context,
                                                              "scale",
                                                              input=True)
        result.required_grad_names[
            "B"] = butils.add_backward_desc_for_connector(nsdfg,
                                                          forward_node,
                                                          context,
                                                          "B",
                                                          input=True)

        # input X
        new_X_desc = copy.deepcopy(X_desc)
        new_X_desc.transient = False
        nsdfg.add_datadesc("X", new_X_desc)

        # input scale
        new_scale_desc = copy.deepcopy(scale_desc)
        new_scale_desc.transient = False
        nsdfg.add_datadesc("scale", new_scale_desc)

        # saved vars
        for saved in ["saved_mean", "saved_var"]:
            saved_desc = copy.deepcopy(
                butils.forward_out_desc_with_name(forward_node, context,
                                                  saved))
            saved_desc.transient = False
            nsdfg.add_datadesc(saved, saved_desc)

        # setup state
        nstate = nsdfg.add_state()
        fwd_unique_id = "{}_{}_{}_{}".format(
            clean_onnx_name(forward_node.name), context.forward_sdfg.sdfg_id,
            context.forward_sdfg.node_id(context.forward_state),
            context.forward_state.node_id(forward_node))

        unique_id = f"{fwd_unique_id}_bwd"

        init, exit = cudnn_implementations._cudnn_tensor_descriptor_code(
            new_X_desc, f"{unique_id}_X_desc", False)
        init_code = init
        finalize_code = exit

        dX_desc = nsdfg.arrays[result.required_grad_names["X"]]
        init, exit = cudnn_implementations._cudnn_tensor_descriptor_code(
            dX_desc, f"{unique_id}_dX_desc", False)
        init_code += init
        finalize_code += exit

        dY_desc = nsdfg.arrays[result.given_grad_names["Y"]]
        init, exit = cudnn_implementations._cudnn_tensor_descriptor_code(
            dY_desc, f"{unique_id}_dY_desc", False)
        init_code += init
        finalize_code += exit

        # setup scale descriptor
        init_code += f"""
        __state->{unique_id}_dScale_desc = new cudnnTensorDescriptor_t; 
        daceml::cudnn::CheckCudnnError(cudnnCreateTensorDescriptor(__state->{unique_id}_dScale_desc));
        daceml::cudnn::CheckCudnnError(cudnnDeriveBNTensorDescriptor(
            *__state->{unique_id}_dScale_desc,
            *__state->{unique_id}_X_desc,
            CUDNN_BATCHNORM_SPATIAL));
        """
        finalize_code += f"""
        daceml::cudnn::CheckCudnnError(cudnnDestroyTensorDescriptor(*__state->{unique_id}_dScale_desc));
        delete __state->{unique_id}_dScale_desc;
        """

        # setup workspace
        init_code += \
            f"""
        {donnx.environments.cuDNN.handle_setup_code(forward_node, init_stream=False)}
        // Setup workspace and reserved space for {unique_id}
        size_t ws_size;
        daceml::cudnn::CheckCudnnError(cudnnGetBatchNormalizationBackwardExWorkspaceSize(
            __dace_cudnn_handle,
            CUDNN_BATCHNORM_SPATIAL,
            CUDNN_BATCHNORM_OPS_BN,
            *__state->{unique_id}_X_desc,
            nullptr,
            *__state->{unique_id}_dY_desc,
            nullptr,
            *__state->{unique_id}_dX_desc,
            *__state->{unique_id}_dScale_desc,
            nullptr,
            &ws_size));
        __state->{unique_id}_workspace_size = new size_t;
        *__state->{unique_id}_workspace_size = ws_size;
        cudaMalloc(&__state->{unique_id}_workspace, ws_size);
        """
        finalize_code += f"""
        delete __state->{unique_id}_workspace_size;
        cudaFree(__state->{unique_id}_workspace);
        """

        tasklet_code = f"""
        {donnx.environments.cuDNN.handle_setup_code(forward_node)}
        float alpha = 1.f;
        float beta = 0.f;
        daceml::cudnn::CheckCudnnError(cudnnBatchNormalizationBackwardEx(
            __dace_cudnn_handle,
            CUDNN_BATCHNORM_SPATIAL,
            CUDNN_BATCHNORM_OPS_BN,
            &alpha,
            &beta,
            &alpha,
            &beta,
            *__state->{unique_id}_X_desc,
            _X,
            nullptr,
            nullptr,
            *__state->{unique_id}_dY_desc,
            _dY,
            nullptr,
            nullptr,
            *__state->{unique_id}_dX_desc,
            _dX,
            *__state->{unique_id}_dScale_desc,
            _scale,
            nullptr,
            _dScale,
            _dBias,
            {forward_node.epsilon},
            _saved_mean,
            _saved_var,
            nullptr,
            __state->{unique_id}_workspace,
            *__state->{unique_id}_workspace_size,
            _reserved_ptr,
            _reserved_size
            ));
        """

        in_connectors = {
            "X": dace.pointer(T),
            "dY": dace.pointer(T),
            "scale": dace.pointer(T),
            "saved_mean": dace.pointer(T),
            "saved_var": dace.pointer(T),
            "reserved_ptr": dace.pointer(dace.typeclass(None)),
            # Here we assume size_t is int64. Unfortunately this is a bad hack and not true
            "reserved_size": dace.int64
        }
        out_connectors = ["dX", "dScale", "dBias"]

        init_code = "{\n" + init_code + "\n}"
        finalize_code = "{\n" + finalize_code + "\n}"
        tasklet = nstate.add_tasklet(
            unique_id, {f"_{i}": t
                        for i, t in in_connectors.items()},
            {f"_{i}": dace.pointer(T)
             for i in out_connectors},
            tasklet_code,
            dace.dtypes.Language.CPP,
            code_init=init_code,
            code_exit=finalize_code,
            state_fields=[
                f"cudnnTensorDescriptor_t *{unique_id}_X_desc;",
                f"cudnnFilterDescriptor_t *{unique_id}_W_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dX_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dY_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dB_desc;",
                f"cudnnFilterDescriptor_t *{unique_id}_dW_desc;",
                f"cudnnTensorDescriptor_t *{unique_id}_dScale_desc;",
                f"cudnnConvolutionDescriptor_t *{unique_id}_conv_desc;",
                f"float *{unique_id}_workspace;",
                f"size_t *{unique_id}_workspace_size;"
            ])
        tasklet.environments = {donnx.environments.cuDNN.full_class_path()}

        # connect inputs
        arr_name = result.given_grad_names["Y"]
        nstate.add_edge(nstate.add_read(arr_name), None, tasklet, f"_dY",
                        nsdfg.make_array_memlet(arr_name))

        for arr_name in ["X", "saved_mean", "scale", "saved_var"]:
            nstate.add_edge(nstate.add_read(arr_name), None, tasklet,
                            f"_{arr_name}", nsdfg.make_array_memlet(arr_name))

        # connect outputs
        arr_name = result.required_grad_names["X"]
        nstate.add_edge(tasklet, "_dX", nstate.add_write(arr_name), None,
                        nsdfg.make_array_memlet(arr_name))
        arr_name = result.required_grad_names["scale"]
        nstate.add_edge(tasklet, "_dScale", nstate.add_write(arr_name), None,
                        nsdfg.make_array_memlet(arr_name))
        arr_name = result.required_grad_names["B"]
        nstate.add_edge(tasklet, "_dBias", nstate.add_write(arr_name), None,
                        nsdfg.make_array_memlet(arr_name))

        # after differentiation, but before validation, we must lower the fwd node,
        # giving the argument that tells it that we need the reserved_ptr output
        def expand(_):
            class Expansion(xf.ExpandTransformation):
                environments = CudnnBatchNormalizationTraining.environments

                @classmethod
                def expansion(cls, node, state, sdfg):
                    return CudnnBatchNormalizationTraining.forward(
                        forward_node,
                        context.forward_state,
                        context.forward_sdfg,
                        reserved_ptr=True)

                @staticmethod
                def annotates_memlets() -> bool:
                    return True

            Expansion._match_node = xf.PatternNode(
                donnx.ONNXBatchNormalization)
            Expansion.apply_to(context.forward_sdfg,
                               verify=False,
                               _match_node=forward_node)

        context.backward_generator.completion_hooks.append(expand)

        # forward the opaque ptr and size
        assert forward_node.add_out_connector("reserved_ptr")
        assert forward_node.add_out_connector("reserved_size")
        reserved_ptr_name, reserved_desc = context.forward_sdfg.add_scalar(
            f"reserved_ptr",
            dace.pointer(dace.typeclass(None)),
            storage=dtypes.StorageType.CPU_Heap,
            find_new_name=True)

        reserved_size_name, reserved_size_desc = context.forward_sdfg.add_scalar(
            f"reserved_size",
            dace.int64,
            storage=dtypes.StorageType.CPU_Heap,
            find_new_name=True)

        context.forward_state.add_edge(
            forward_node, "reserved_ptr",
            context.forward_state.add_read(reserved_ptr_name), None,
            dace.Memlet(f"{reserved_ptr_name}[0]"))
        context.forward_state.add_edge(
            forward_node, "reserved_size",
            context.forward_state.add_read(reserved_size_name), None,
            dace.Memlet(f"{reserved_size_name}[0]"))

        bwd_reserved_desc = copy.deepcopy(reserved_desc)
        bwd_reserved_desc.transient = False
        bwd_reserved_size_desc = copy.deepcopy(reserved_size_desc)
        bwd_reserved_size_desc.transient = False
        nsdfg.add_datadesc("reserved_ptr", bwd_reserved_desc)
        nsdfg.add_datadesc("reserved_size", bwd_reserved_size_desc)
        nstate.add_edge(nstate.add_read("reserved_ptr"), None, tasklet,
                        "_reserved_ptr", dace.Memlet("reserved_ptr[0]"))
        nstate.add_edge(nstate.add_read("reserved_size"), None, tasklet,
                        "_reserved_size", dace.Memlet("reserved_size[0]"))

        node = context.backward_state.add_nested_sdfg(
            nsdfg, None, {
                "X", result.given_grad_names["Y"], "scale", "saved_mean",
                "saved_var", "reserved_ptr", "reserved_size"
            }, {result.required_grad_names[a]
                for a in {"X", "scale", "B"}})

        butils.connect_output_from_forward(forward_node, node, context,
                                           "saved_mean")
        butils.connect_output_from_forward(forward_node, node, context,
                                           "saved_var")

        butils.connect_output_from_forward(forward_node, node, context,
                                           "reserved_ptr")
        butils.connect_output_from_forward(forward_node, node, context,
                                           "reserved_size")

        return node, result


@autoregister_params(op="GlobalAveragePool", name="pure")
class PureGlobalAveragePoolingBackward(BackwardImplementation):
    @staticmethod
    def backward_can_be_applied(node: nd.Node, state: dace.SDFGState,
                                sdfg: dace.SDFG) -> bool:
        return len(utils.in_desc_with_name(node, state, sdfg, "X").shape) == 4

    @staticmethod
    def backward(
        forward_node: nd.Node, context: BackwardContext,
        given_gradients: List[Optional[str]],
        required_gradients: List[Optional[str]]
    ) -> Tuple[nd.Node, BackwardResult]:
        desc = butils.forward_in_desc_with_name(forward_node, context, "X")
        N, C, H, W = desc.shape
        dtype = desc.dtype

        inv = 1.0 / (H * W)

        def bwd(X_grad, Y_grad):
            for n, c, h, w in dace.map[0:N, 0:C, 0:H, 0:W]:
                with dace.tasklet:
                    y_grad << Y_grad[n, c]
                    x_grad >> X_grad[n, c, h, w]
                    x_grad = y_grad * dtype(inv)

        return butils.backward_program_for_node(bwd, context, forward_node)
