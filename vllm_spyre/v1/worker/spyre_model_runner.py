import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, TypeVar

import torch
from torch import nn
from vllm.config import DeviceConfig, VllmConfig
from vllm.logger import init_logger
from vllm.sampling_params import SamplingType
from vllm.utils import is_pin_memory_available
from vllm.v1.kv_cache_interface import FullAttentionSpec, KVCacheSpec
from vllm.v1.outputs import SamplerOutput
from vllm.v1.sample.metadata import SamplingMetadata
from vllm.worker.model_runner_base import (
    ModelRunnerBase, ModelRunnerInputBase,
    _add_sampling_metadata_broadcastable_dict,
    _init_sampling_metadata_from_tensor_dict)

from vllm_spyre.model_executor.model_loader.spyre import SpyreCausalLM
from vllm_spyre.platform import SpyrePlatform
from vllm_spyre.v1.worker.spyre_input_batch import (CachedRequestState,
                                                    InputBatch)

if TYPE_CHECKING:
    from vllm.attention.backends.abstract import AttentionBackend
    from vllm.model_executor.pooling_metadata import PoolingMetadata

    from vllm_spyre.v1.core.sched.output import (CachedRequestData,
                                                 NewRequestData,
                                                 SchedulerOutput)
else:
    CachedRequestData = None
    SchedulerOutput = None
    NewRequestData = None

from vllm.v1.outputs import ModelRunnerOutput

import vllm_spyre.envs as envs_spyre

logger = init_logger(__name__)

TModelInputForSpyre = TypeVar('TModelInputForSpyre',
                              bound="ModelInputForSpyre")


@dataclass(frozen=True)
class ModelInputForSpyre(ModelRunnerInputBase):
    """
    Used by the SpyreModelRunner.
    """
    input_tokens: Optional[torch.Tensor] = None
    input_positions: Optional[torch.Tensor] = None
    input_masks: Optional[torch.Tensor] = None
    sampling_metadata: Optional[SamplingMetadata] = None
    pooling_metadata: Optional["PoolingMetadata"] = None
    is_prompt: Optional[bool] = None
    # unused
    virtual_engine: Optional[int] = None

    def as_broadcastable_tensor_dict(self) -> dict[str, Any]:
        tensor_dict = {
            "input_tokens": self.input_tokens,
            "input_positions": self.input_positions,
            "input_masks": self.input_masks,
            "is_prompt": self.is_prompt,
        }
        _add_sampling_metadata_broadcastable_dict(tensor_dict,
                                                  self.sampling_metadata)
        return tensor_dict

    @classmethod
    def from_broadcasted_tensor_dict(
        cls: type[TModelInputForSpyre],
        tensor_dict: dict[str, Any],
        attn_backend: Optional["AttentionBackend"] = None,
    ) -> TModelInputForSpyre:
        tensor_dict = _init_sampling_metadata_from_tensor_dict(tensor_dict)
        return cls(**tensor_dict)


class SpyreModelRunner(ModelRunnerBase[ModelInputForSpyre]):

    def __init__(
        self,
        vllm_config: VllmConfig,
        is_driver_worker: bool,
    ):
        super().__init__(vllm_config=vllm_config)
        self.is_driver_worker = is_driver_worker

        self.pad_token_id = 0
        if self.model_config is not None:
            if self.model_config.hf_config is not None:
                self.pad_token_id = getattr(self.model_config.hf_config,
                                            "pad_token_id", None) or 0
            if self.model_config.get_sliding_window():
                logger.warning("Sliding window is not supported on Spyre. "
                               "The model will run without sliding window.")
        if vllm_config.device_config is None:
            self.device_config = DeviceConfig()
        self.device = self.device_config.device
        self.pin_memory = is_pin_memory_available()

        # Lazy initialization: after load_model.
        self.model: nn.Module

    def get_model(self) -> nn.Module:
        return self.model

    def load_model(self, prompt_lens: Iterable[int],
                   num_decode_tokens: Iterable[int]) -> None:
        max_pad_length = max(prompt_lens)
        max_decode_length = max(num_decode_tokens)
        self.model = SpyreCausalLM(self.model_config,
                                   parallel_config=self.parallel_config,
                                   max_prompt_length=max_pad_length,
                                   max_decode_length=max_decode_length)

    @property
    def vocab_size(self) -> int:
        return self.model.model.model.config.src_vocab_size

    def make_model_input_from_broadcasted_tensor_dict(
            self, tensor_dict: dict[str, Any]) -> ModelInputForSpyre:
        return ModelInputForSpyre.from_broadcasted_tensor_dict(tensor_dict)

    def _prepare_pad_input_ids(
        self,
        input_ids_list: list[torch.Tensor],
        min_pad_length: int = 0,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """left side padding implemented as
        in fms.utils.generation.pad_input_id"""
        max_len = max([min_pad_length] +
                      [seq.size(0) for seq in input_ids_list])
        padded_input_ids_list = []
        mask_list = []
        position_ids_list = []
        for input_ids_i in input_ids_list:
            seq_len = input_ids_i.size(0)
            if max_len > seq_len:
                logger.info(
                    "Padding request of length %d tokens to %d tokens.",
                    seq_len, max_len)
            pads = torch.ones(max_len - seq_len,
                              dtype=torch.long,
                              device=input_ids_i.device) * self.pad_token_id
            non_pads = torch.ones(seq_len,
                                  dtype=torch.long,
                                  device=input_ids_i.device)

            pos_ids_pads = pads
            pos_ids_seq = torch.arange(0,
                                       seq_len,
                                       dtype=torch.long,
                                       device=input_ids_i.device)

            # Setting this to 0, however if 0 is the eos, we will end up
            # truncating the output if using truncate_after_eos once this
            # workflow works for nested tensor, this can probably be removed
            padded_input_ids_list.append(torch.cat((pads, input_ids_i)))
            mask_list.append(torch.cat((torch.zeros_like(pads), non_pads)))
            position_ids_list.append(torch.cat((pos_ids_pads, pos_ids_seq)))

        return padded_input_ids_list, mask_list, position_ids_list

    def pad_input_ids(
        self,
        input_ids_list: list[torch.Tensor],
        min_pad_length: int = 0,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:

        padded_input_ids_list, mask_list, position_ids_list = self.\
            _prepare_pad_input_ids(input_ids_list, min_pad_length)

        input_ids = torch.stack(padded_input_ids_list)
        mask = torch.stack(mask_list).bool()
        # this is a causal mask for generation
        mask = (mask.unsqueeze(-1) == mask.unsqueeze(-2)).tril()
        mask = torch.where(mask.logical_not(), -torch.inf, 0.0)
        mask = mask.to(self.model.model.dtype)
        position_ids = torch.stack(position_ids_list)

        return input_ids, position_ids, mask

    def get_kv_cache_spec(self) -> KVCacheSpec:
        """
        This method should generate the KVCache spec by parsing the kv cache
        format from each Attention module in the static forward context.

        In vLLM, this static forward context is populated by the base Attention
        class in the modeling code. Every attention layer populates an entry
        for itself in vllm_config.compilation_config.static_forward_context,
        which is a dictionary of layer_name -> layer for every attention layer.
        This allows the model runner to correctly create the kv cache spec for
        each layer.

        The spyre modeling code currently comes from `fms`, and does not
        integrate with vLLM's modeling classes, so we don't have access to any
        model-agnostic metadata about the attention layers. This just returns a
        dummy value for now.
        """
        # We do at least use the real size from the cache config.
        block_size = self.vllm_config.cache_config.block_size

        attn_spec = FullAttentionSpec(block_size=block_size,
                                      num_kv_heads=1,
                                      head_size=1,
                                      dtype=torch.float16,
                                      use_mla=False)
        return {"foo": attn_spec}


class StaticBatchingSpyreModelRunner(SpyreModelRunner):

    def __init__(
        self,
        vllm_config: VllmConfig,
        is_driver_worker: bool,
    ):
        super().__init__(vllm_config=vllm_config,
                         is_driver_worker=is_driver_worker)

        # position_ids of all the sequences in current batch
        self._position_ids: torch.Tensor = None
        # attention masks of all the sequences in current batch
        self._mask: torch.Tensor = None

        # Batch state
        self.input_batch = InputBatch(
            max_num_reqs=vllm_config.scheduler_config.max_num_seqs,
            max_model_len=vllm_config.model_config.max_model_len,
            device=self.device,
            pin_memory=self.pin_memory,
            vocab_size=vllm_config.model_config.get_vocab_size(),
        )

        # Requests
        self.requests: dict[str, CachedRequestData] = {}

        self.spyre_warmup_shapes = SpyrePlatform.get_warmup_shapes(
            self.scheduler_config)

    def _prepare_prompt(
        self,
        new_requests: list[NewRequestData],
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, list[int]]:
        assert len(new_requests) > 0
        input_token_list: list[torch.Tensor] = []
        padded_batch_size, min_pad_length_batch = \
            self._get_padded_batch_size(new_requests)

        # Internal state is reset here.
        # We don't support continuous batching, so we know all previous requests
        # have finished decoding.
        self.input_batch.clear_requests()
        self.requests = {}

        # Build batch and prepare input_token1
        for request_data in new_requests:
            # retrieve initial (unpadded) tokens
            prompt_tokens = request_data.prompt_token_ids

            input_token_list.append(
                torch.tensor(prompt_tokens,
                             dtype=torch.long,
                             device=torch.device("cpu")))

            # Add new requests to the cached states.
            req_id = request_data.req_id
            sampling_params = request_data.sampling_params
            if sampling_params.sampling_type == SamplingType.RANDOM_SEED:
                generator = torch.Generator(device=self.device)
                generator.manual_seed(sampling_params.seed)
            else:
                generator = None

            req_state = CachedRequestState(
                req_id=req_id,
                prompt_token_ids=request_data.prompt_token_ids,
                prompt=request_data.prompt,
                sampling_params=sampling_params,
                generator=generator,
                output_token_ids=[],
            )
            self.requests[req_id] = req_state
            self.input_batch.add_request(req_state)

        self.input_batch.padded_batch_size = padded_batch_size

        # Refresh sampling metadata after all request are added to the batch
        self.input_batch.refresh_sampling_metadata()

        # padding to compiled batch size
        while len(input_token_list) < padded_batch_size:
            input_token_list.append(
                torch.zeros(min_pad_length_batch,
                            dtype=torch.long,
                            device=torch.device("cpu")))

        # get position ids and attention mask
        input_tokens, self._position_ids, self._mask = self.pad_input_ids(
            input_token_list, min_pad_length=min_pad_length_batch)

        seq_lens = [t.shape[0] for t in input_token_list]

        return input_tokens, self._position_ids, self._mask, seq_lens

    def _prepare_decode(
        self,
        cached_requests: list[CachedRequestData],
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        assert len(cached_requests) > 0
        input_tokens: list[list[int]] = [
            [0] for _ in range(self._position_ids.shape[0])
        ]

        for cached_request in cached_requests:
            # TODO: Will this always just be one token ID if there's no spec
            # or jump decoding?
            generation_token = cached_request.new_token_ids[-1]
            input_tokens[self.input_batch.req_id_to_index[
                cached_request.req_id]] = [generation_token]

        # update position ids and attention mask
        self._update_position_ids()
        self._update_mask()

        input_tokens = torch.tensor(input_tokens,
                                    dtype=torch.long,
                                    device=self.device)

        return input_tokens, self._position_ids, self._mask

    def _update_position_ids(self) -> None:
        """Updating the position ids of all sequences
        in a batch. Will be called in decoding phase"""

        self._position_ids = self._position_ids[:, -1] + 1
        self._position_ids = self._position_ids.unsqueeze(-1)

    def _update_mask(self) -> None:
        """Updating/extending the attention masks of all
        sequences in a batch. Will be called in decoding phase"""

        assert self._mask is not None
        masks = self._mask

        masks_new = []
        for mask in masks:
            # get the last row of the 3d mask
            mask_new = mask[-1:, :]

            # extend the mask one slot
            mask_new = torch.cat(
                (
                    mask_new,
                    torch.zeros(
                        1, 1, dtype=mask_new.dtype, device=mask_new.device),
                ),
                dim=1,
            )
            masks_new.append(mask_new)

        self._mask = torch.stack(masks_new, dim=0)

    def prepare_model_input(
            self, scheduler_output: SchedulerOutput) -> ModelInputForSpyre:

        # NOTE: We assume that all sequences in the group are all prompts or
        # all decodes.
        # Also assuming that new sequences are prefills
        is_prompt = len(scheduler_output.scheduled_new_reqs) > 0

        # Prepare input tensors.
        if is_prompt:
            # Assert no running requests
            assert len(scheduler_output.scheduled_cached_reqs) == 0

            (input_tokens, input_positions, input_masks,
             _) = self._prepare_prompt(scheduler_output.scheduled_new_reqs)
        else:
            if scheduler_output.finished_req_ids:
                for req_id in scheduler_output.finished_req_ids:
                    self.input_batch.soft_remove_request(req_id)
                self.input_batch.refresh_sampling_metadata()

            (input_tokens, input_positions, input_masks) = \
                self._prepare_decode(scheduler_output.scheduled_cached_reqs)

        sampling_metadata = self.input_batch.sampling_metadata

        return ModelInputForSpyre(input_tokens=input_tokens,
                                  input_positions=input_positions,
                                  input_masks=input_masks,
                                  sampling_metadata=sampling_metadata,
                                  is_prompt=is_prompt)

    @SpyrePlatform.inference_mode()
    def execute_model(
        self,
        scheduler_output: SchedulerOutput,
        **kwargs,
    ) -> ModelRunnerOutput:

        t0 = time.time()

        # TODO: change to EMPTY_MODEL_RUNNER_OUTPUT, right now this
        # will be a breaking change, or clumsy to make retrocompatible
        # with conditional import
        if not scheduler_output.total_num_scheduled_tokens:
            # Return empty ModelRunnerOuptut if there's no work to do.
            return ModelRunnerOutput(
                req_ids=[],
                req_id_to_index={},
                sampled_token_ids=[],
                spec_token_ids=None,
                logprobs=None,
                prompt_logprobs_dict={},
            )

        self._update_states(scheduler_output)

        model_input = self.prepare_model_input(scheduler_output)

        # TODO(Wallas): I think it would be better move the indices as argument
        # of the forward rather than set as an attribute of the model. I'm not
        # sure how easy is that right now.

        # Always get the indices from the input_batch
        self.model.indices = self.input_batch.get_model_indices()

        # Execute the model
        hidden_states = self.model(
            input_ids=model_input.input_tokens,
            positions=model_input.input_positions,
            masks=model_input.input_masks,
            is_prompt=model_input.is_prompt,
        )

        # Only perform sampling in the driver worker.
        if not self.is_driver_worker:
            return []

        # Compute the logits.
        logits = self.model.compute_logits(hidden_states, None)

        # Sample the next token.
        output: SamplerOutput = self.model.sample(
            logits=logits,
            sampling_metadata=model_input.sampling_metadata,
        )
        t1 = time.time() - t0
        logger.debug("t_token: %.2fms", (t1 * 1000))

        model_output = ModelRunnerOutput(
            req_ids=self.input_batch.requests_ids,
            req_id_to_index=self.input_batch.get_unpadded_output_indices(),
            sampled_token_ids=output.sampled_token_ids.tolist(),
            spec_token_ids=None,
            logprobs=output.logprobs_tensors.tolists()
            if output.logprobs_tensors else None,
            prompt_logprobs_dict={
                req_id: None
                for req_id in self.input_batch.req_id_to_index
            }  # TODO(wallas?): prompt logprobs too
        )
        return model_output

    def _update_states(self, scheduler_output: SchedulerOutput):
        # Update the states of the running/resumed requests.
        # For now, we are updating input_batch.'s `token_ids_cpu`,
        # `num_tokens`
        #
        # NOTE: req_state.output_token_ids is being mutated.
        #
        # Once we have continuous batch, we shall update more data
        for req_data in scheduler_output.scheduled_cached_reqs:
            req_id = req_data.req_id
            req_state = self.requests[req_id]

            # Update the cached states.
            num_computed_tokens = req_data.num_computed_tokens
            req_state.num_computed_tokens = num_computed_tokens
            # Add the sampled token(s) from the previous step (if any).
            # This doesn't include "unverified" tokens like spec decode tokens.
            num_new_tokens = (num_computed_tokens +
                              len(req_data.new_token_ids) -
                              req_state.num_tokens)
            if num_new_tokens == 1:
                # Avoid slicing list in most common case.
                req_state.output_token_ids.append(req_data.new_token_ids[-1])
            elif num_new_tokens > 0:
                req_state.output_token_ids.extend(
                    req_data.new_token_ids[-num_new_tokens:])

            req_index = self.input_batch.get_req_index(req_id)
            # Add new_token_ids to token_ids_cpu.
            # TODO: Update for spec decoding in the future
            start_token_index = num_computed_tokens
            end_token_index = num_computed_tokens + len(req_data.new_token_ids)
            self.input_batch.token_ids_cpu[
                req_index,
                start_token_index:end_token_index] = req_data.new_token_ids

    def _get_padded_batch_size(self, new_requests: list[NewRequestData]):
        # find warmup shape to be used for padding and batching
        applicable_spyre_warmup_shapes = [
            shape for shape in self.spyre_warmup_shapes
            if len(new_requests) <= shape['batch_size']
        ]
        for request_data in new_requests:
            # retrieve initial (unpadded) tokens
            prompt_tokens = request_data.prompt_token_ids
            new_tokens = request_data.sampling_params.max_tokens\
                if request_data.sampling_params is not None else 0

            updated_spyre_warmup_shapes = [
                shape for shape in applicable_spyre_warmup_shapes
                if len(prompt_tokens) <= shape['prompt_length']
                and new_tokens <= shape['new_tokens']
            ]
            applicable_spyre_warmup_shapes = updated_spyre_warmup_shapes

        assert applicable_spyre_warmup_shapes, \
            "No shapes available to run prefill batch. (This should not happen)"

        # If multiple warmup shapes apply, the first one is selected.
        # For improving performance, the warmup shapes in scheduler_config
        # are ordered by "processing speed".
        min_pad_length_batch = applicable_spyre_warmup_shapes[0][
            'prompt_length']
        padded_batch_size = applicable_spyre_warmup_shapes[0]['batch_size']
        return padded_batch_size, min_pad_length_batch


class ContinuousBatchingSpyreModelRunner(SpyreModelRunner):

    def __init__(
        self,
        vllm_config: VllmConfig,
        is_driver_worker: bool,
    ):
        super().__init__(vllm_config=vllm_config,
                         is_driver_worker=is_driver_worker)

        max_batch_size = envs_spyre.VLLM_SPYRE_MAX_BATCH_SIZE
        # this is just to pass formatting bc type is Optional[list[int]]
        if envs_spyre.VLLM_SPYRE_WARMUP_PROMPT_LENS:
            max_prompt_length = envs_spyre.VLLM_SPYRE_WARMUP_PROMPT_LENS[0]

        # TO DO: move to InputBatch
        self.req_ids2page: dict = {}
        self.active_pages: list[int] = []
        self.tkv = 0
        self.free_pages = [i for i in range(max_batch_size)]
        self.min_pad_length_batch = max_prompt_length

    def _prepare_prompt(
        self,
        new_requests: list[NewRequestData],
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, list[int]]:
        assert len(new_requests) > 0
        input_token_list: list[torch.Tensor] = []

        # Internal state is managed here.
        self.active_pages = []
        for request_data in new_requests:
            free_page_idx = self.free_pages.pop(0)
            self.active_pages.append(free_page_idx)
            self.req_ids2page[request_data.req_id] = free_page_idx

            # retrieve initial (unpadded) tokens
            prompt_tokens = request_data.prompt_token_ids

            input_token_list.append(
                torch.tensor(prompt_tokens,
                             dtype=torch.long,
                             device=torch.device("cpu")))

        # prefils are always of batch size 1 for this milestone
        actual_batch_size = len(input_token_list)
        assert actual_batch_size == 1
        self.model.indices = torch.ones(actual_batch_size,
                                        dtype=torch.bool,
                                        device='cpu')

        if self.tkv == 0:
            self.tkv = self.min_pad_length_batch

        # get position ids and attention mask
        input_tokens, position_ids, mask =\
            self.pad_input_ids(input_token_list, min_pad_length=self.tkv)

        seq_lens = [t.shape[0] for t in input_token_list]

        return input_tokens, position_ids, mask, seq_lens

    def _prepare_decode(
        self,
        cached_requests: list[CachedRequestData],
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        assert len(cached_requests) > 0
        input_tokens = []
        self.active_pages = []
        self.model.indices = torch.ones(len(cached_requests),
                                        dtype=torch.bool,
                                        device='cpu')

        for cached_request in cached_requests:
            # TODO: Will this always just be one token ID if there's no spec
            # or jump decoding?
            self.active_pages.append(self.req_ids2page[cached_request.req_id])
            generation_token = cached_request.new_token_ids[-1]
            input_tokens.append([generation_token])

        input_tokens = torch.tensor(input_tokens,
                                    dtype=torch.long,
                                    device=self.device)
        mask, position_ids = self._prepare_pos_mask_decode(
            cached_requests, self.tkv)
        self.tkv = self.tkv + 1

        return input_tokens, position_ids, mask

    def _prepare_pos_mask_decode(
        self,
        cached_requests: list[CachedRequestData],
        tkv: int = 0,
    ) -> tuple[torch.Tensor, torch.Tensor]:

        mask_list = []
        position_ids_list = []

        for cached_request in cached_requests:
            seq_len = cached_request.num_computed_tokens
            position_ids_list.append([seq_len])

            pads = torch.ones(tkv - seq_len,
                              dtype=torch.long,
                              device=self.device) * self.pad_token_id
            non_pads = torch.ones(seq_len + 1,
                                  dtype=torch.long,
                                  device=self.device)
            mask_list.append(torch.cat((torch.zeros_like(pads), non_pads)))

        mask = torch.stack(mask_list).bool()
        mask = torch.where(mask.logical_not(), -torch.inf, 0.0)
        mask = mask.to(self.model.model.dtype)
        mask = torch.unsqueeze(mask, dim=1)
        position_ids = torch.tensor(position_ids_list,
                                    dtype=torch.long,
                                    device=self.device)
        return mask, position_ids

    def prepare_model_input(
            self, scheduler_output: SchedulerOutput) -> ModelInputForSpyre:

        # NOTE: We assume that all sequences in the group are all prompts or
        # all decodes.
        # Also assuming that new sequences are prefills
        is_prompt = len(scheduler_output.scheduled_new_reqs) > 0

        for req_id in scheduler_output.finished_req_ids:
            if req_id in self.req_ids2page:
                self.free_pages.append(self.req_ids2page[req_id])
                del self.req_ids2page[req_id]

        # Prepare input tensors.
        if is_prompt:
            (input_tokens, input_positions, input_masks,
             _) = self._prepare_prompt(scheduler_output.scheduled_new_reqs)
            num_reqs = len(scheduler_output.scheduled_new_reqs)
        else:
            (input_tokens, input_positions, input_masks) = \
                self._prepare_decode(scheduler_output.scheduled_cached_reqs)
            num_reqs = len(scheduler_output.scheduled_cached_reqs)

        # TODO: Build the rest of the SamplingMetadata correctly
        dummy_tensors = lambda v: torch.full(
            (num_reqs, ), v, device=self.device)
        dummy_metadata = SamplingMetadata(
            temperature=dummy_tensors(0.0),
            all_greedy=False,
            all_random=False,
            top_p=None,
            top_k=None,
            min_p=None,
            generators={},
            max_num_logprobs=None,
            no_penalties=True,
            prompt_token_ids=None,
            frequency_penalties=dummy_tensors(0.1),
            presence_penalties=dummy_tensors(0.1),
            repetition_penalties=dummy_tensors(0.1),
            output_token_ids=[[] for _ in range(num_reqs)],
            min_tokens={},
            logit_bias=[None for _ in range(num_reqs)],
            allowed_token_ids_mask=None,
            bad_words_token_ids=None,
        )

        return ModelInputForSpyre(input_tokens=input_tokens,
                                  input_positions=input_positions,
                                  input_masks=input_masks,
                                  sampling_metadata=dummy_metadata,
                                  is_prompt=is_prompt)

    @SpyrePlatform.inference_mode()
    def execute_model(
        self,
        scheduler_output: SchedulerOutput,
        **kwargs,
    ) -> ModelRunnerOutput:

        t0 = time.time()
        model_input = self.prepare_model_input(scheduler_output)

        # Execute the model
        hidden_states = self.model(
            input_ids=model_input.input_tokens,
            positions=model_input.input_positions,
            masks=model_input.input_masks,
            is_prompt=model_input.is_prompt,
            tkv=0 if model_input.is_prompt else self.tkv,
            active_pages=self.active_pages,
        )

        # Only perform sampling in the driver worker.
        if not self.is_driver_worker:
            return []

        # Compute the logits.
        logits = self.model.compute_logits(hidden_states, None)

        # Sample the next token.
        output: SamplerOutput = self.model.sample(
            logits=logits,
            sampling_metadata=model_input.sampling_metadata,
        )
        t1 = time.time() - t0
        logger.debug("t_token: %.2fms", (t1 * 1000))

        is_prompt = len(scheduler_output.scheduled_new_reqs) > 0
        scheduled_req = scheduler_output.scheduled_new_reqs if is_prompt\
            else scheduler_output.scheduled_cached_reqs
        # since same order as in _prepare_prompt/decode req_ids2idx not needed
        req_ids = [req.req_id for req in scheduled_req]
        req_id_to_index = {req_id: i for i, req_id in enumerate(req_ids)}

        model_output = ModelRunnerOutput(
            req_ids=req_ids,
            req_id_to_index=req_id_to_index,
            sampled_token_ids=output.sampled_token_ids.tolist(),
            spec_token_ids=None,
            logprobs=output.logprobs_tensors.tolists()
            if output.logprobs_tensors else None,
            prompt_logprobs_dict={req_id: None
                                  for req_id in req_ids
                                  }  # TODO(wallas?): prompt logprobs too
        )
        return model_output
