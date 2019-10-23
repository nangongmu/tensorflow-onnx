# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""Unit Test Base."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging
import os
import unittest

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import numpy as np
import tensorflow as tf
TF2 = tf.__version__.startswith("2.")
from tensorflow.python.ops import variables as variables_lib
from common import get_test_config
from tf2onnx import utils
from tf2onnx.tfonnx import process_tf_graph
from tf2onnx import optimizer
from tf2onnx.tf_loader import tf_optimize, tf_reset_default_graph, tf_session, tf_placeholder, freeze_func


# pylint: disable=missing-docstring,invalid-name,unused-argument,using-constant-test, import-outside-toplevel

class Tf2OnnxBackendTestBase(unittest.TestCase):
    def setUp(self):
        self.config = get_test_config()
        tf_reset_default_graph()
        # reset name generation on every test
        utils.INTERNAL_NAME = 1
        np.random.seed(1)  # Make it reproducible.
        self.logger = logging.getLogger(self.__class__.__name__)

    def tearDown(self):
        if not self.config.is_debug_mode:
            utils.delete_directory(self.test_data_directory)

    @property
    def test_data_directory(self):
        return os.path.join(self.config.temp_dir, self._testMethodName)

    @staticmethod
    def assertAllClose(expected, actual, **kwargs):
        np.testing.assert_allclose(expected, actual, **kwargs)

    @staticmethod
    def assertAllEqual(expected, actual, **kwargs):
        np.testing.assert_array_equal(expected, actual, **kwargs)

    def run_onnxcaffe2(self, onnx_graph, inputs):
        """Run test against caffe2 backend."""
        import caffe2.python.onnx.backend
        prepared_backend = caffe2.python.onnx.backend.prepare(onnx_graph)
        results = prepared_backend.run(inputs)
        return results

    def run_onnxruntime(self, model_path, inputs, output_names):
        """Run test against onnxruntime backend."""
        import onnxruntime as rt
        m = rt.InferenceSession(model_path)
        results = m.run(output_names, inputs)
        return results

    def run_backend(self, g, outputs, input_dict):
        model_proto = g.make_model("test")
        model_path = self.save_onnx_model(model_proto, input_dict)

        if self.config.backend == "onnxruntime":
            y = self.run_onnxruntime(model_path, input_dict, outputs)
        elif self.config.backend == "caffe2":
            y = self.run_onnxcaffe2(model_proto, input_dict)
        else:
            raise ValueError("unknown backend")
        return y

    def run_test_case(self, func, feed_dict, input_names_with_port, output_names_with_port, rtol=1e-07, atol=1e-5,
                      convert_var_to_const=True, constant_fold=True, check_value=True, check_shape=True,
                      check_dtype=True, process_args=None, onnx_feed_dict=None, graph_validator=None):
        # optional - passed to process_tf_graph
        if process_args is None:
            process_args = {}
        # optional - pass distinct feed_dict to onnx runtime
        if onnx_feed_dict is None:
            onnx_feed_dict = feed_dict
        input_names_with_port = list(feed_dict)
        tf_reset_default_graph()
        graph_def = None

        clean_feed_dict = {utils.node_name(k): v for k, v in feed_dict.items()}
        input_tensors = [tf.TensorSpec(shape=v.shape, dtype=tf.as_dtype(v.dtype), name=utils.node_name(k))
                         for k, v in feed_dict.items()]
        if TF2:
            #
            # use eager to execute the tensorflow func
            #
            # numpy doesn't work for all ops, make it tf.Tensor()
            input_list = [tf.convert_to_tensor(v, dtype=tf.as_dtype(v.dtype), name=utils.node_name(k))
                             for k, v in feed_dict.items()]
            expected = func(*input_list)
            if isinstance(expected, list) or isinstance(expected, tuple):
                # list or tuple
                expected = [x.numpy() for x in expected]
            else:
                # single result
                expected = [expected.numpy()]

            # now make the eager functions a graph
            concrete_func = tf.function(func, input_signature=tuple(input_tensors))
            concrete_func = concrete_func.get_concrete_function()
            graph_def = freeze_func(concrete_func, output_names_with_port)
        else:
            #
            # use graph to execute the tensorflow func
            #
            with tf_session() as sess:
                input_list = []
                for k, v in clean_feed_dict.items():
                    input_list.append(tf_placeholder(name=k, shape=v.shape, dtype=tf.as_dtype(v.dtype)))
                func(*input_list)
                variables_lib.global_variables_initializer().run()
                output_dict = []
                for out_name in output_names_with_port:
                    output_dict.append(sess.graph.get_tensor_by_name(out_name))
                expected = sess.run(output_dict, feed_dict=feed_dict)
                graph_def = sess.graph_def

            if convert_var_to_const:
                with tf_session() as sess:
                    variables_lib.global_variables_initializer().run()
                    output_name_without_port = [n.split(':')[0] for n in output_names_with_port]
                    graph_def = tf_optimize(input_names_with_port, output_names_with_port, graph_def, constant_fold)

        tf_reset_default_graph()
        tf.import_graph_def(graph_def, name='')

        if self.config.is_debug_mode:
            model_path = os.path.join(self.test_data_directory, self._testMethodName + "_after_tf_optimize.pb")
            utils.save_protobuf(model_path, graph_def)
            self.logger.debug("created file  %s", model_path)

        with tf_session() as sess:
            g = process_tf_graph(sess.graph, opset=self.config.opset, output_names=output_names_with_port,
                                 target=self.config.target, **process_args)
            g = optimizer.optimize_graph(g)
            actual = self.run_backend(g, output_names_with_port, onnx_feed_dict)

        for expected_val, actual_val in zip(expected, actual):
            if check_value:
                self.assertAllClose(expected_val, actual_val, rtol=rtol, atol=atol)
            if check_dtype:
                self.assertEqual(expected_val.dtype, actual_val.dtype)
            # why need shape checke: issue when compare [] with scalar
            # https://github.com/numpy/numpy/issues/11071
            if check_shape:
                self.assertEqual(expected_val.shape, actual_val.shape)

        if graph_validator:
            self.assertTrue(graph_validator(g))

        return g

    def save_onnx_model(self, model_proto, feed_dict, postfix=""):
        target_path = utils.save_onnx_model(self.test_data_directory, self._testMethodName + postfix, feed_dict,
                                            model_proto, include_test_data=self.config.is_debug_mode,
                                            as_text=self.config.is_debug_mode)

        self.logger.debug("create model file: %s", target_path)
        return target_path
