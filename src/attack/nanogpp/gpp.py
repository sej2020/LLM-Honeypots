import copy
import gc
import logging
import queue
import threading

from dataclasses import dataclass
from tqdm import tqdm
from typing import List, Optional, Tuple, Union

import torch
import transformers
from torch import Tensor
from transformers import set_seed
from scipy.stats import spearmanr

from src.attack.nanogpp.nano_utils import (
    INIT_CHARS,
    configure_pad_token,
    find_executable_batch_size,
    get_nonascii_toks,
    mellowmax,
)

logger = logging.getLogger("nanogpp")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@dataclass
class ProbeSamplingConfig:
    draft_model: transformers.PreTrainedModel
    draft_tokenizer: transformers.PreTrainedTokenizer
    r: int = 8
    sampling_factor: int = 16


@dataclass
class GCGConfig:
    num_steps: int = 250
    optim_str_init: Union[str, List[str]] = "x x x x x x x x x x x x x x x x x x x x"
    search_width: int = 512
    batch_size: int = None
    topk: int = 256
    n_replace: int = 1
    buffer_size: int = 0
    universal: bool = False
    use_mellowmax: bool = False
    mellowmax_alpha: float = 1.0
    early_stop: bool = False
    use_prefix_cache: bool = True
    allow_non_ascii: bool = False
    filter_ids: bool = True
    add_space_before_target: bool = False
    seed: int = None
    verbosity: str = "INFO"
    probe_sampling_config: Optional[ProbeSamplingConfig] = None


@dataclass
class GCGResult:
    best_loss: float
    best_string: str
    losses: List[float]
    strings: List[str]


class AttackBuffer:
    def __init__(self, size: int):
        self.buffer = []  # elements are (loss: float, optim_ids: Tensor)
        self.size = size

    def add(self, loss: float, optim_ids: Tensor) -> None:
        if self.size == 0:
            self.buffer = [(loss, optim_ids)]
            return

        if len(self.buffer) < self.size:
            self.buffer.append((loss, optim_ids))
        else:
            self.buffer[-1] = (loss, optim_ids)

        self.buffer.sort(key=lambda x: x[0])

    def get_best_ids(self) -> Tensor:
        return self.buffer[0][1]

    def get_lowest_loss(self) -> float:
        return self.buffer[0][0]

    def get_highest_loss(self) -> float:
        return self.buffer[-1][0]

    def log_buffer(self, tokenizer):
        message = "buffer:"
        for loss, ids in self.buffer:
            optim_str = tokenizer.batch_decode(ids)[0]
            optim_str = optim_str.replace("\\", "\\\\")
            optim_str = optim_str.replace("\n", "\\n")
            message += f"\nloss: {loss}" + f" | string: {optim_str}"
        logger.info(message)


def sample_ids_from_grad(
    ids: Tensor,
    grad: Tensor,
    search_width: int,
    topk: int = 256,
    n_replace: int = 1,
    not_allowed_ids: Tensor = False,
):
    """Returns `search_width` combinations of token ids based on the token gradient.

    Args:
        ids : Tensor, shape = (n_optim_ids)
            the sequence of token ids that are being optimized
        grad : Tensor, shape = (n_optim_ids, vocab_size)
            the gradient of the GCG loss computed with respect to the one-hot token embeddings
        search_width : int
            the number of candidate sequences to return
        topk : int
            the topk to be used when sampling from the gradient
        n_replace : int
            the number of token positions to update per sequence
        not_allowed_ids : Tensor, shape = (n_ids)
            the token ids that should not be used in optimization

    Returns:
        sampled_ids : Tensor, shape = (search_width, n_optim_ids)
            sampled token ids
    """
    n_optim_tokens = len(ids)
    original_ids = ids.repeat(search_width, 1)

    if not_allowed_ids is not None:
        grad[:, not_allowed_ids.to(grad.device)] = float("inf")

    # gradient descent
    topk_ids = (-grad).topk(topk, dim=1).indices

    sampled_ids_pos = torch.argsort(torch.rand((search_width, n_optim_tokens), device=grad.device))[..., :n_replace]
    sampled_ids_val = torch.gather(
        topk_ids[sampled_ids_pos],
        2,
        torch.randint(0, topk, (search_width, n_replace, 1), device=grad.device),
    ).squeeze(2)

    new_ids = original_ids.scatter_(1, sampled_ids_pos, sampled_ids_val)

    return new_ids


def filter_ids(ids: Tensor, tokenizer: transformers.PreTrainedTokenizer):
    """Filters out sequeneces of token ids that change after retokenization.

    Args:
        ids : Tensor, shape = (search_width, n_optim_ids)
            token ids
        tokenizer : ~transformers.PreTrainedTokenizer
            the model's tokenizer

    Returns:
        filtered_ids : Tensor, shape = (new_search_width, n_optim_ids)
            all token ids that are the same after retokenization
    """
    ids_decoded = tokenizer.batch_decode(ids)
    filtered_ids = []

    for i in range(len(ids_decoded)):
        # Retokenize the decoded token ids
        ids_encoded = tokenizer(ids_decoded[i], return_tensors="pt", add_special_tokens=False).to(ids.device)["input_ids"][0]
        if torch.equal(ids[i], ids_encoded):
            filtered_ids.append(ids[i])

    if not filtered_ids:
        # This occurs in some cases, e.g. using the Llama-3 tokenizer with a bad initialization
        raise RuntimeError(
            "No token sequences are the same after decoding and re-encoding. "
            "Consider setting `filter_ids=False` or trying a different `optim_str_init`"
        )

    return torch.stack(filtered_ids)


class GCG:
    def __init__(
        self,
        model: transformers.PreTrainedModel,
        tokenizer: transformers.PreTrainedTokenizer,
        config: GCGConfig,
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config

        self.embedding_layer = model.get_input_embeddings()
        self.not_allowed_ids = None if config.allow_non_ascii else get_nonascii_toks(tokenizer, device=model.device)
        
        # don't know what these are yet
        self.prefix_cache = None
        self.draft_prefix_cache = None
        self.stop_flag = False
        self.draft_model = None
        self.draft_tokenizer = None
        self.draft_embedding_layer = None
        
        if self.config.probe_sampling_config:
            self.draft_model = self.config.probe_sampling_config.draft_model
            self.draft_tokenizer = self.config.probe_sampling_config.draft_tokenizer
            self.draft_embedding_layer = self.draft_model.get_input_embeddings()
            if self.draft_tokenizer.pad_token is None:
                configure_pad_token(self.draft_tokenizer)

        if model.dtype in (torch.float32, torch.float64):
            logger.warning(f"Model is in {model.dtype}. Use a lower precision data type, if possible, for much faster optimization.")

        if model.device == torch.device("cpu"):
            logger.warning("Model is on the CPU. Use a hardware accelerator for faster optimization.")

        if not tokenizer.chat_template:
            logger.warning("Tokenizer does not have a chat template. Assuming base model and setting chat template to empty.")
            tokenizer.chat_template = "{% for message in messages %}{{ message['content'] }}{% endfor %}"

    def run(
        self,
        messages: Union[str, List[dict], List[str]],
        target: Union[str, List[str]],
    ) -> GCGResult:
        model = self.model
        tokenizer = self.tokenizer
        config = self.config

        self.prompt_index = 0

        if config.seed is not None:
            set_seed(config.seed)
            torch.use_deterministic_algorithms(True, warn_only=True)

        if config.universal:
            messages = [[{"role": "user", "content": mess}] for mess in messages] # list of conversations - each conversation is a list of messages
        else:
            if isinstance(messages, str):
                messages = [[{"role": "user", "content": messages}]] # single conversation
            else:
                messages = copy.deepcopy(messages)

        # Append the GCG string at the end of the prompt if location not specified
        # Assert optim_str is present if universal optimization
        for conversation in messages:
            if not any(["{optim_str}" in d["content"] for d in conversation]):
                if config.universal:
                    raise ValueError("GCG string ({optim_str}) must be present in the messages for universal optimization.")
                else:
                    messages[-1]["content"] = messages[-1]["content"] + "{optim_str}"

        targets = target if config.universal else [target]

        # making messages and targets in a list so I can put them both in the same for loop

        before_embeds_list = []
        after_embeds_list = []
        target_embeds_list = []
        target_ids_list = []

        for message, targ in zip(messages, targets):
            template = tokenizer.apply_chat_template(message, tokenize=False, add_generation_prompt=True)
            # Remove the BOS token -- this will get added when tokenizing, if necessary
            if tokenizer.bos_token and template.startswith(tokenizer.bos_token):
                template = template.replace(tokenizer.bos_token, "")
            before_str, after_str = template.split("{optim_str}")

            targ = " " + targ if config.add_space_before_target else targ

            # Tokenize everything that doesn't get optimized
            before_ids = tokenizer([before_str], padding=False, return_tensors="pt")["input_ids"].to(model.device, torch.int64)
            after_ids = tokenizer([after_str], add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device, torch.int64)
            target_ids = tokenizer([targ], add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device, torch.int64)

            # Embed everything that doesn't get optimized
            embedding_layer = self.embedding_layer
            # [1, len(seq), embed_dim]
            before_embeds, after_embeds, target_embeds = [embedding_layer(ids) for ids in (before_ids, after_ids, target_ids)]

            # Compute the KV Cache for tokens that appear before the optimized tokens
            if config.use_prefix_cache:
                with torch.no_grad():
                    output = model(inputs_embeds=before_embeds, use_cache=True)
                    self.prefix_cache = output.past_key_values

            before_embeds_list.append(before_embeds)
            after_embeds_list.append(after_embeds)
            target_embeds_list.append(target_embeds)
            target_ids_list.append(target_ids)


        self.target_ids_list = target_ids_list
        self.before_embeds_list = before_embeds_list
        self.after_embeds_list = after_embeds_list
        self.target_embeds_list = target_embeds_list

        

        # Initialize components for probe sampling, if enabled.
        if config.probe_sampling_config:
            assert self.draft_model and self.draft_tokenizer and self.draft_embedding_layer, "Draft model wasn't properly set up."

            # Tokenize everything that doesn't get optimized for the draft model
            draft_before_ids = self.draft_tokenizer([before_str], padding=False, return_tensors="pt")["input_ids"].to(model.device, torch.int64)
            draft_after_ids = self.draft_tokenizer([after_str], add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device, torch.int64)
            self.draft_target_ids = self.draft_tokenizer([target], add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device, torch.int64)

            (
                self.draft_before_embeds,
                self.draft_after_embeds,
                self.draft_target_embeds,
            ) = [
                self.draft_embedding_layer(ids)
                for ids in (
                    draft_before_ids,
                    draft_after_ids,
                    self.draft_target_ids,
                )
            ]

            if config.use_prefix_cache:
                with torch.no_grad():
                    output = self.draft_model(inputs_embeds=self.draft_before_embeds, use_cache=True)
                    self.draft_prefix_cache = output.past_key_values

        # Initialize the attack buffer
        buffer = self.init_buffer()
        optim_ids = buffer.get_best_ids() # starting with the trigger ids

        losses = []
        optim_strings = []

        for _ in tqdm(range(config.num_steps)):
            # Compute the gradients for every possible token at every position - this is linearized loss approximation
            optim_ids_onehot_grad = self.compute_token_gradient(optim_ids)

            with torch.no_grad():

                # Sample candidate token sequences based on the token gradient - [search_width, n_optim_ids]
                sampled_ids = sample_ids_from_grad(
                    optim_ids.squeeze(0),
                    optim_ids_onehot_grad.squeeze(0),
                    config.search_width,
                    config.topk,
                    config.n_replace,
                    not_allowed_ids=self.not_allowed_ids,
                )

                if config.filter_ids:
                    sampled_ids = filter_ids(sampled_ids, tokenizer)

                new_search_width = sampled_ids.shape[0]

                total_loss = torch.zeros(new_search_width, device=model.device, dtype=model.dtype)
                induced_target_all = torch.zeros((self.prompt_index+1,new_search_width), device=model.device, dtype=torch.bool)

                for i in range(self.prompt_index+1):
                    # Compute loss on all candidate sequences
                    batch_size = new_search_width if config.batch_size is None else config.batch_size
                    if self.prefix_cache:
                        input_embeds = torch.cat([
                            embedding_layer(sampled_ids),
                            after_embeds_list[i].repeat(new_search_width, 1, 1),
                            target_embeds_list[i].repeat(new_search_width, 1, 1),
                        ], dim=1)
                    else:
                        input_embeds = torch.cat([
                            before_embeds_list[i].repeat(new_search_width, 1, 1),
                            embedding_layer(sampled_ids),
                            after_embeds_list[i].repeat(new_search_width, 1, 1),
                            target_embeds_list[i].repeat(new_search_width, 1, 1),
                        ], dim=1)

                    if self.config.probe_sampling_config is None:
                        # compute loss on all candidate sequences for a single prompt
                        loss, induced_target = find_executable_batch_size(self._compute_candidates_loss_original, batch_size)(input_embeds, self.target_ids_list[i])
                        total_loss = total_loss + loss
                        induced_target_all[i] = induced_target

                    else:
                        current_loss, optim_ids = find_executable_batch_size(self._compute_candidates_loss_probe_sampling, batch_size)(
                            input_embeds, sampled_ids,
                        )

                # select the best candidate sequence
                av_loss = total_loss / (self.prompt_index + 1)
                current_loss = av_loss.min().item()
                optim_ids = sampled_ids[av_loss.argmin()].unsqueeze(0)

                # Update the buffer based on the loss
                losses.append(current_loss)
                if buffer.size == 0 or current_loss < buffer.get_highest_loss():
                    buffer.add(current_loss, optim_ids)

            optim_ids = buffer.get_best_ids()
            optim_str = tokenizer.batch_decode(optim_ids)[0]
            optim_strings.append(optim_str)

            buffer.log_buffer(tokenizer)

            if config.early_stop and not config.universal:
                if torch.any(induced_target_all[-1]).item():
                    logger.info("Early stopping triggered.")
                    break
                
            if config.universal:
                induced_all_targets = torch.all(induced_target_all, dim=0)
                if induced_all_targets[av_loss.argmin()]: # we have succeeded at attacking all prompts up until now
                    self.prompt_index += 1
                if self.prompt_index == len(messages): # this means we have succeed at attacking all prompt
                    logger.info("Early stopping triggered.")
                    break

        min_loss_index = losses.index(min(losses))

        result = GCGResult(
            best_loss=losses[min_loss_index],
            best_string=optim_strings[min_loss_index],
            losses=losses,
            strings=optim_strings,
        )

        return result

    def init_buffer(self) -> AttackBuffer:
        model = self.model
        tokenizer = self.tokenizer
        config = self.config

        logger.info(f"Initializing attack buffer of size {config.buffer_size}...")

        # Create the attack buffer and initialize the buffer ids
        buffer = AttackBuffer(config.buffer_size)

        if isinstance(config.optim_str_init, str):
            init_optim_ids = tokenizer(config.optim_str_init, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
            if config.buffer_size > 1:
                init_buffer_ids = tokenizer(INIT_CHARS, add_special_tokens=False, return_tensors="pt")["input_ids"].squeeze().to(model.device)
                init_indices = torch.randint(0, init_buffer_ids.shape[0], (config.buffer_size - 1, init_optim_ids.shape[1]))
                init_buffer_ids = torch.cat([init_optim_ids, init_buffer_ids[init_indices]], dim=0)
            else:
                init_buffer_ids = init_optim_ids

        else:  # assume list
            if len(config.optim_str_init) != config.buffer_size:
                logger.warning(f"Using {len(config.optim_str_init)} initializations but buffer size is set to {config.buffer_size}")
            try:
                init_buffer_ids = tokenizer(config.optim_str_init, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
            except ValueError:
                logger.error("Unable to create buffer. Ensure that all initializations tokenize to the same length.")

        true_buffer_size = max(1, config.buffer_size)

        # Compute the loss on the initial buffer entries
        if self.prefix_cache:
            init_buffer_embeds = torch.cat([
                self.embedding_layer(init_buffer_ids),
                self.after_embeds_list[0].repeat(true_buffer_size, 1, 1),
                self.target_embeds_list[0].repeat(true_buffer_size, 1, 1),
            ], dim=1)
        else:
            init_buffer_embeds = torch.cat([
                self.before_embeds_list[0].repeat(true_buffer_size, 1, 1),
                self.embedding_layer(init_buffer_ids),
                self.after_embeds_list[0].repeat(true_buffer_size, 1, 1),
                self.target_embeds_list[0].repeat(true_buffer_size, 1, 1),
            ], dim=1)

        # gets loss on the initial prompt+trigger+prompt+target. Target is added just as an efficient way to get autoregressive logits
        init_buffer_losses, _ = find_executable_batch_size(self._compute_candidates_loss_original, true_buffer_size)(init_buffer_embeds, self.target_ids_list[0])

        # Populate the buffer
        for i in range(true_buffer_size):
            buffer.add(init_buffer_losses[i], init_buffer_ids[[i]])

        buffer.log_buffer(tokenizer)

        logger.info("Initialized attack buffer.")

        return buffer

    def compute_token_gradient(
        self,
        optim_ids: Tensor,
    ) -> Tensor:
        """Computes the gradient of the GCG loss w.r.t the one-hot token matrix.

        Args:
            optim_ids : Tensor, shape = (1, n_optim_ids)
                the sequence of token ids that are being optimized
        """
        model = self.model
        embedding_layer = self.embedding_layer

        # Create the one-hot encoding matrix of our optimized token ids
        optim_ids_onehot = torch.nn.functional.one_hot(optim_ids, num_classes=embedding_layer.num_embeddings)
        optim_ids_onehot = optim_ids_onehot.to(model.device, model.dtype)
        optim_ids_onehot.requires_grad_()

        # (1, num_optim_tokens, vocab_size) @ (vocab_size, embed_dim) -> (1, num_optim_tokens, embed_dim)
        optim_embeds = optim_ids_onehot @ embedding_layer.weight

        total_loss = torch.zeros(optim_ids.shape[0], device=model.device, dtype=model.dtype)

        for i in range(self.prompt_index+1):
            if self.prefix_cache:
                input_embeds = torch.cat([optim_embeds, self.after_embeds_list[i], self.target_embeds_list[i]], dim=1)
                output = model(
                    inputs_embeds=input_embeds,
                    past_key_values=self.prefix_cache,
                    use_cache=True,
                )
            else:
                input_embeds = torch.cat(
                    [
                        self.before_embeds_list[i],
                        optim_embeds,
                        self.after_embeds_list[i],
                        self.target_embeds_list[i],
                    ],
                    dim=1,
                ) # [batch_size, seq_len, embed_dim]
                output = model(inputs_embeds=input_embeds)

            logits = output.logits

            # Shift logits so token n-1 predicts token n
            shift = input_embeds.shape[1] - self.target_ids_list[i].shape[1]
            shift_logits = logits[..., shift - 1 : -1, :].contiguous()  # (1, num_target_ids, vocab_size)
            shift_labels = self.target_ids_list[i]

            if self.config.use_mellowmax:
                label_logits = torch.gather(shift_logits, -1, shift_labels.unsqueeze(-1)).squeeze(-1)
                loss = mellowmax(-label_logits, alpha=self.config.mellowmax_alpha, dim=-1)
            else:
                loss = torch.nn.functional.cross_entropy(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

            total_loss = total_loss + loss

        av_loss = total_loss / (self.prompt_index+1)

        optim_ids_onehot_grad = torch.autograd.grad(outputs=[av_loss], inputs=[optim_ids_onehot])[0]

        return optim_ids_onehot_grad

    def _compute_candidates_loss_original(
        self,
        search_batch_size: int,
        input_embeds: Tensor,
        target_ids: Tensor,
    ) -> Tensor:
        """Computes the GCG loss on all candidate token id sequences.

        Args:
            search_batch_size : int
                the number of candidate sequences to evaluate in a given batch
            input_embeds : Tensor, shape = (search_width, seq_len, embd_dim)
                the embeddings of the `search_width` candidate sequences to evaluate
            target_ids : Tensor, shape = (1, seq_len)
                the token ids of the target sequence
        """
        all_loss = []
        prefix_cache_batch = []

        for i in range(0, input_embeds.shape[0], search_batch_size):
            with torch.no_grad():
                input_embeds_batch = input_embeds[i:i + search_batch_size]
                current_batch_size = input_embeds_batch.shape[0]

                if self.prefix_cache:
                    if not prefix_cache_batch or current_batch_size != search_batch_size:
                        prefix_cache_batch = [[x.expand(current_batch_size, -1, -1, -1) for x in self.prefix_cache[i]] for i in range(len(self.prefix_cache))]

                    outputs = self.model(inputs_embeds=input_embeds_batch, past_key_values=prefix_cache_batch, use_cache=True)
                else:
                    outputs = self.model(inputs_embeds=input_embeds_batch)

                logits = outputs.logits

                tmp = input_embeds.shape[1] - target_ids.shape[1]
                shift_logits = logits[..., tmp-1:-1, :].contiguous()
                shift_labels = target_ids.repeat(current_batch_size, 1)

                # this could be where we add CW loss. Seems to be pretty straightforward. Really anywhere use_mellowmax is used
                if self.config.use_mellowmax:
                    label_logits = torch.gather(shift_logits, -1, shift_labels.unsqueeze(-1)).squeeze(-1)
                    loss = mellowmax(-label_logits, alpha=self.config.mellowmax_alpha, dim=-1)
                else:
                    loss = torch.nn.functional.cross_entropy(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1), reduction="none")

                loss = loss.view(current_batch_size, -1).mean(dim=-1)
                all_loss.append(loss)

                # do we induce the target with any candidate?
                induced_target = torch.all(torch.argmax(shift_logits, dim=-1) == shift_labels, dim=-1)

                del outputs
                gc.collect()
                torch.cuda.empty_cache()

        return torch.cat(all_loss, dim=0), induced_target


    def _compute_candidates_loss_probe_sampling(
        self,
        search_batch_size: int,
        input_embeds: Tensor,
        sampled_ids: Tensor,
    ) -> Tuple[float, Tensor]:
        """Computes the GCG loss using probe sampling (https://arxiv.org/abs/2403.01251).

        Args:
            search_batch_size : int
                the number of candidate sequences to evaluate in a given batch
            input_embeds : Tensor, shape = (search_width, seq_len, embd_dim)
                the embeddings of the `search_width` candidate sequences to evaluate
            sampled_ids: Tensor, all candidate token id sequences. Used for further sampling.

        Returns:
            A tuple of (min_loss: float, corresponding_sequence: Tensor)

        """
        probe_sampling_config = self.config.probe_sampling_config
        assert probe_sampling_config, "Probe sampling config wasn't set up properly."

        B = input_embeds.shape[0]
        probe_size = B // probe_sampling_config.sampling_factor
        probe_idxs = torch.randperm(B)[:probe_size].to(input_embeds.device)
        probe_embeds = input_embeds[probe_idxs]

        def _compute_probe_losses(result_queue: queue.Queue, search_batch_size: int, probe_embeds: Tensor) -> None:
            probe_losses = self._compute_candidates_loss_original(search_batch_size, probe_embeds)
            result_queue.put(("probe", probe_losses))

        def _compute_draft_losses(
            result_queue: queue.Queue,
            search_batch_size: int,
            draft_sampled_ids: Tensor,
        ) -> None:
            assert self.draft_model and self.draft_embedding_layer, "Draft model and embedding layer weren't initialized properly."

            draft_losses = []
            draft_prefix_cache_batch = None
            for i in range(0, B, search_batch_size):
                with torch.no_grad():
                    batch_size = min(search_batch_size, B - i)
                    draft_sampled_ids_batch = draft_sampled_ids[i : i + batch_size]

                    if self.draft_prefix_cache:
                        if not draft_prefix_cache_batch or batch_size != search_batch_size:
                            draft_prefix_cache_batch = [
                                [x.expand(batch_size, -1, -1, -1) for x in self.draft_prefix_cache[i]] for i in range(len(self.draft_prefix_cache))
                            ]
                        draft_embeds = torch.cat(
                            [
                                self.draft_embedding_layer(draft_sampled_ids_batch),
                                self.draft_after_embeds.repeat(batch_size, 1, 1),
                                self.draft_target_embeds.repeat(batch_size, 1, 1),
                            ],
                            dim=1,
                        )
                        draft_output = self.draft_model(
                            inputs_embeds=draft_embeds,
                            past_key_values=draft_prefix_cache_batch,
                        )
                    else:
                        draft_embeds = torch.cat(
                            [
                                self.draft_before_embeds.repeat(batch_size, 1, 1),
                                self.draft_embedding_layer(draft_sampled_ids_batch),
                                self.draft_after_embeds.repeat(batch_size, 1, 1),
                                self.draft_target_embeds.repeat(batch_size, 1, 1),
                            ],
                            dim=1,
                        )
                        draft_output = self.draft_model(inputs_embeds=draft_embeds)

                    draft_logits = draft_output.logits
                    tmp = draft_embeds.shape[1] - self.draft_target_ids.shape[1]
                    shift_logits = draft_logits[..., tmp - 1 : -1, :].contiguous()
                    shift_labels = self.draft_target_ids.repeat(batch_size, 1)

                    if self.config.use_mellowmax:
                        label_logits = torch.gather(shift_logits, -1, shift_labels.unsqueeze(-1)).squeeze(-1)
                        loss = mellowmax(-label_logits, alpha=self.config.mellowmax_alpha, dim=-1)
                    else:
                        loss = (
                            torch.nn.functional.cross_entropy(
                                shift_logits.view(-1, shift_logits.size(-1)),
                                shift_labels.view(-1),
                                reduction="none",
                            )
                            .view(batch_size, -1)
                            .mean(dim=-1)
                        )

                    draft_losses.append(loss)

            draft_losses = torch.cat(draft_losses)
            result_queue.put(("draft", draft_losses))

        def _convert_to_draft_tokens(token_ids: Tensor) -> Tensor:
            decoded_text_list = self.tokenizer.batch_decode(token_ids)
            assert self.draft_tokenizer, "Draft tokenizer wasn't properly initialized."
            return self.draft_tokenizer(
                decoded_text_list,
                add_special_tokens=False,
                padding=True,
                return_tensors="pt",
            )[
                "input_ids"
            ].to(self.draft_model.device, torch.int64)

        result_queue = queue.Queue()
        draft_sampled_ids = _convert_to_draft_tokens(sampled_ids)

        # Step 1. Compute loss of all candidates using the draft model
        draft_thread = threading.Thread(
            target=_compute_draft_losses,
            args=(result_queue, search_batch_size, draft_sampled_ids),
        )

        # Step 2. In parallel to 1., compute loss of the probe set on target model
        probe_thread = threading.Thread(
            target=_compute_probe_losses,
            args=(result_queue, search_batch_size, probe_embeds),
        )

        draft_thread.start()
        probe_thread.start()

        draft_thread.join()
        probe_thread.join()

        results = {}
        while not result_queue.empty():
            key, value = result_queue.get()
            results[key] = value

        probe_losses = results["probe"]
        draft_losses = results["draft"]

        # Step 3. Calculate agreement score using Spearman correlation
        draft_probe_losses = draft_losses[probe_idxs]
        rank_correlation = spearmanr(
            probe_losses.cpu().type(torch.float32).numpy(),
            draft_probe_losses.cpu().type(torch.float32).numpy(),
        ).correlation
        # normalized from [-1, 1] to [0, 1]
        alpha = (1 + rank_correlation) / 2

        # Step 4. Calculate the filtered set and evaluate using the target model.
        R = probe_sampling_config.r
        filtered_size = int((1 - alpha) * B / R)
        filtered_size = max(1, min(filtered_size, B))

        _, top_indices = torch.topk(draft_losses, k=filtered_size, largest=False)

        filtered_embeds = input_embeds[top_indices]
        filtered_losses = self._compute_candidates_loss_original(search_batch_size, filtered_embeds)

        # Step 5. Return best loss between probe set and filtered set
        best_probe_loss = probe_losses.min().item()
        best_filtered_loss = filtered_losses.min().item()

        probe_ids = sampled_ids[probe_idxs]
        filtered_ids = sampled_ids[top_indices]
        return (
            (best_probe_loss, probe_ids[probe_losses.argmin()].unsqueeze(0))
            if best_probe_loss < best_filtered_loss
            else (
                best_filtered_loss,
                filtered_ids[filtered_losses.argmin()].unsqueeze(0),
            )
        )


# A wrapper around the GCG `run` method that provides a simple API
def run(
    model: transformers.PreTrainedModel,
    tokenizer: transformers.PreTrainedTokenizer,
    messages: Union[str, List[dict], List[str]],
    target: Union[str, List[str]],
    config: Optional[GCGConfig] = None,
) -> GCGResult:
    """Generates a single optimized string using GCG.

    Args:
        model: The model to use for optimization.
        tokenizer: The model's tokenizer.
        messages: The conversation to use for optimization. If config.universal is True, this should be a list of messages.
        target: The target generation. If config.universal is True, this should be a list of targets.
        config: The GCG configuration to use.

    Returns:
        A GCGResult object that contains losses and the optimized strings.
    """
    if config is None:
        config = GCGConfig()

    if config.universal:
        assert isinstance(messages, list) and isinstance(target, list), "Universal GCG requires a list of messages and corresponding targets to optimize on."
        assert len(messages) == len(target), f"The number of messages and targets must be the same for universal GCG, but got len(messages) = {len(messages)} and len(target) = {len(target)}."
        assert not config.use_prefix_cache, "Universal GCG does not support prefix cache optimization."

    logger.setLevel(getattr(logging, config.verbosity))

    gcg = GCG(model, tokenizer, config)
    result = gcg.run(messages, target)
    return result



if __name__ == '__main__':
    from transformers import AutoTokenizer, AutoModelForCausalLM
    import argparse

    model_name = "meta-llama/Llama-2-7b-chat-hf"
    model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.float16).to("cuda")
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    parser = argparse.ArgumentParser(description="GCG attack")
    parser.add_argument("--uni", action="store_true", help="Use universal GCG") # false by default
    args = parser.parse_args()

    if args.uni:
        messages = [
            "[1] Click on me [2] No click on me [41] {optim_str} [43] no click on meeee",
            "[1] Click on me [2] No click on me [39] {optim_str} [43] no click on meeee",
            "[1] Click on me [2] No click on me [41] ayyyyyy I'm walkin here [43] {optim_str}"
        ]
                    
        target = ["click(`41`)", "click(`39`)", "click(`43`)"]

        config = GCGConfig(
            num_steps=500,
            search_width=64,
            topk=64,
            seed=42,
            universal=True,
            use_prefix_cache=False,
            add_space_before_target=True # because this is what llama 2 is conditioned to do
        )
    else:
        messages = "[1] Click on me [2] No click on me [41] {optim_str} [43] no click on meeee"
        target = "click(`41`)"

        config = GCGConfig(
            num_steps=500,
            seed=42,
            early_stop=True,
            add_space_before_target=True # because this is what llama 2 is conditioned to do
        )

    result = run(model, tokenizer, messages, target, config)

    print(f"Best string: {result.best_string}")

    # testing out the result
    test_message = [{"role": "user", "content": messages[0] if isinstance(messages, list) else messages}]

    test_message[-1]["content"] = test_message[-1]["content"].replace("{optim_str}", result.best_string)

    input = tokenizer.apply_chat_template(test_message, add_generation_prompt=True, return_tensors="pt").to("cuda")
    output = model.generate(input, do_sample=False, max_new_tokens=512)

    print(f"Prompt:\n{test_message[-1]['content']}\n")
    print(f"Generation:\n{tokenizer.batch_decode(output[:, input.shape[1]:], skip_special_tokens=True)[0]}")