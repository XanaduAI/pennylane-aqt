# Copyright 2020 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Alpine Quantum Technologies device class
========================================

**Module name:** :mod:`pennylane_aqt.AQTDevice`

.. currentmodule:: pennylane_aqt.AQTDevice

An abstract base class for constructing AQT devices for PennyLane.

Classes
-------

.. autosummary::
   AQTDevice

Code details
~~~~~~~~~~~~
"""
import os
import json

import numpy as np
from pennylane import QubitDevice, DeviceError

from ._version import __version__
from .api_client import join_path, submit, valid_status_code, raise_invalid_status_exception

BASE_SHOTS = 200


class AQTDevice(QubitDevice):
    r"""AQT device for PennyLane.

    Args:
        wires (int): the number of wires to initialize the device with
        shots (int): Number of circuit evaluations/random samples used
            to estimate expectation values of observables.
        api_key (str): The AQT API key. If not provided, the environment
            variable ``AQT_TOKEN`` is used.
    """
    #pylint: disable=too-many-instance-attributes
    name = "AQT Simulator PennyLane plugin"
    pennylane_requires = ">=0.9.0"
    version = __version__
    author = "Xanadu Inc."
    _capabilities = {
        "model": "qubit",
        "tensor_observables": True,
        "inverse_operations": True,
    }

    short_name = "aqt.base_device"
    _operation_map = {
        # native PennyLane operations also native to AQT
        "RX": "X",
        "RY": "Y",
        "RZ": "Z",
        # operations not natively implemented in AQT
        "BasisState": None,
        "PauliX": None,
        "PauliY": None,
        "PauliZ": None,
        "Hadamard": None,
        # additional operations not native to PennyLane but present in AQT
        "R": "R",
        "MS": "MS",
    }

    observables = {"PauliX", "PauliY", "PauliZ", "Identity", "Hadamard", "Hermitian"}

    BASE_HOSTNAME = "https://gateway.aqt.eu/marmot"
    TARGET_PATH = ""
    HTTP_METHOD = "PUT"

    def __init__(self, wires, shots=BASE_SHOTS, api_key=None):
        super().__init__(wires=wires, shots=shots, analytic=False)
        self._initial_shots = shots
        self._api_key = api_key
        self.circuit = []
        self.circuit_json = ""
        self.samples = None
        self.set_api_configs()

    def reset(self):
        """Reset the device and reload configurations."""
        self.shots = self._initial_shots
        self.circuit = []
        self.circuit_json = ""
        self.samples = None
        self.set_api_configs()

    def set_api_configs(self):
        """
        Set the configurations needed to connect to AQT API.
        """
        self._api_key = self._api_key or os.getenv("AQT_TOKEN")
        if not self._api_key:
            raise ValueError("No valid api key for AQT platform found.")
        self.header = {"Ocp-Apim-Subscription-Key": self._api_key}
        self.data = {"access_token": self._api_key, "no_qubits": self.num_wires}
        self.hostname = join_path(self.BASE_HOSTNAME, self.TARGET_PATH)

    @property
    def operations(self):
        """Get the supported set of operations.

        Returns:
            set[str]: the set of PennyLane operation names the device supports
        """
        return set(self._operation_map.keys())

    def apply(self, operations, **kwargs):
        rotations = kwargs.pop("rotations", [])

        for i, operation in enumerate(operations):
            if i > 0 and operation.name in {"BasisState", "QubitStateVector"}:
                raise DeviceError(
                    "The operation {} is only supported at the beginning of a circuit.".format(
                        operation.name
                    )
                )
            self._apply_operation(operation)

        # diagonalize observables
        for operation in rotations:
            self._apply_operation(operation)

        # create circuit job for submission
        self.circuit_json = self.serialize(self.circuit)
        self.data["repetitions"] = self.shots
        job_submission = {**self.data, "data": self.circuit_json}
        response = submit(self.HTTP_METHOD, self.hostname, job_submission, self.header)

        # poll for completed job
        if not valid_status_code(response):
            raise_invalid_status_exception(response)
        job = response.json()
        job_query_data = {"id": job["id"], "access_token": self._api_key}
        while job["status"] != "finished":
            # TODO: add timeout
            job = submit(self.HTTP_METHOD, self.hostname, job_query_data, self.header).json()

        self.samples = job["samples"]

    def _apply_operation(self, operation, par=None, wires=None):
        """
        Add the specified operation to ``self.circuit`` with the native AQT op name.

        If ``par`` or ``wires`` are not explicitly specified, they are pulled from
        the attributes of ``operation``.

        Args:
            operation[pennylane.operation.Operation]: the operation instance to be applied
            par[float, None]: the numerical parameter of the operation
            wires[list[int], None]: which wires to apply the operation to
        """
        op_name = operation.name
        if len(operation.parameters) == 1:
            par = par or operation.parameters[0]
        elif len(operation.parameters) == 2:
            par = par or operation.parameters
        wires = wires or operation.wires

        if op_name == "R":
            self.circuit.append([op_name, par[0], par[1], wires])
            return
        if operation.name == "BasisState":
            for bit, wire in zip(par, wires):
                if bit == 1:
                    self._append_op_to_queue("RX", np.pi, wires=[wire])
            return

        if op_name == "Hadamard":
            op_name = "RY"
            par = 0.5 * np.pi
        elif op_name in ("PauliX", "PauliY", "PauliZ"):
            op_name = "R{}".format(op_name[-1])
            par = np.pi
        elif op_name == "MS":
            par *= np.pi

        self._append_op_to_queue(op_name, par, wires)

    def _append_op_to_queue(self, op_name, par, wires):
        """
        Append the given operation to the circuit queue in the correct format for AQT API.

        Args:
            op_name[str]: the PennyLane name of the op
            par[float]: the numeric parameter value for the op
            wires[list[int]]: the wires the op is to be applied on
        """
        par = par / np.pi  # AQT convention: all gates differ from PennyLane by factor of pi
        aqt_op_name = self._operation_map[op_name]
        self.circuit.append([aqt_op_name, par, wires])

    @staticmethod
    def serialize(circuit):
        """
        Serialize ``circuit`` to a valid AQT-formatted JSON string.

        Args:
             circuit[list[list]]: a list of lists of the form [["X", 0.3, [0]], ["Z", 0.1, [2]], ...]
        """
        return json.dumps(circuit)

    def generate_samples(self):
        # AQT indexes in reverse scheme to PennyLane, so we have to specify "F" ordering
        samples_array = np.stack(np.unravel_index(self.samples, [2] * self.num_wires, order="F")).T
        return samples_array