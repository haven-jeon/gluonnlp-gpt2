import mxnet as mx
import numpy as np
import gluonnlp as nlp
import argparse
import re
from model import load_pretrained_GPT2

def parse_ctx(ctx_args):
    ctx = re.findall('([a-z]+)(\d*)', ctx_args)
    ctx = [(device, int(num)) if len(num) > 0 else (device, 0) for device, num in ctx]
    ctx = [mx.Context(*ele) for ele in ctx]
    return ctx

def _expand_to_beam_size(data, beam_size, batch_size, state_info=None):
    """Tile all the states to have batch_size * beam_size on the batch axis.

    Parameters
    ----------
    data : A single NDArray/Symbol or nested container with NDArrays/Symbol
        Each NDArray/Symbol should have shape (N, ...) when state_info is None,
        or same as the layout in state_info when it's not None.
    beam_size : int
        Beam size
    batch_size : int
        Batch size
    state_info : Nested structure of dictionary, default None.
        Descriptors for states, usually from decoder's ``state_info()``.
        When None, this method assumes that the batch axis is the first dimension.
    Returns
    -------
    new_states : Object that contains NDArrays/Symbols
        Each NDArray/Symbol should have shape batch_size * beam_size on the batch axis.
    """
    assert not state_info or isinstance(state_info, (type(data), dict)), \
            'data and state_info doesn\'t match, ' \
            'got: {} vs {}.'.format(type(state_info), type(data))
    if isinstance(data, list):
        if not state_info:
            state_info = [None] * len(data)
        return [_expand_to_beam_size(d, beam_size, batch_size, s)
                for d, s in zip(data, state_info)]
    elif isinstance(data, tuple):
        if not state_info:
            state_info = [None] * len(data)
            state_info = tuple(state_info)
        return tuple(_expand_to_beam_size(d, beam_size, batch_size, s)
                     for d, s in zip(data, state_info))
    elif isinstance(data, dict):
        if not state_info:
            state_info = {k: None for k in data.keys()}
        return {k: _expand_to_beam_size(v, beam_size, batch_size, state_info[k])
                for k, v in data.items()}
    elif isinstance(data, mx.nd.NDArray):
        if not state_info:
            batch_axis = 0
        else:
            batch_axis = state_info['__layout__'].find('N')
        if data.shape[batch_axis] != batch_size:
            raise ValueError('The batch dimension of all the inner elements in states must be '
                             '{}, Found shape={}'.format(batch_size, data.shape))
        new_shape = list(data.shape)
        new_shape[batch_axis] = batch_size * beam_size
        new_shape = tuple(new_shape)
        return data.expand_dims(batch_axis+1)\
                   .broadcast_axes(axis=batch_axis+1, size=beam_size)\
                   .reshape(new_shape)
    elif isinstance(data, mx.sym.Symbol):
        if not state_info:
            batch_axis = 0
        else:
            batch_axis = state_info['__layout__'].find('N')
        new_shape = (0, ) * batch_axis + (-3, -2)
        return data.expand_dims(batch_axis+1)\
                   .broadcast_axes(axis=batch_axis+1, size=beam_size)\
                   .reshape(new_shape)
    elif data is None:
        return None
    else:
        raise NotImplementedError

class GPT2Decoder(object):
    def __init__(self, gpt2_model):
        self._gpt2_model = gpt2_model

    def __call__(self, inputs, states):
        inputs = mx.nd.expand_dims(inputs, axis=1)
        out, new_states = self._gpt2_model(inputs, states)
        return mx.nd.slice_axis(out, axis=1, begin=0, end=1).reshape((inputs.shape[0], -1)), new_states

nlp.model.sequence_sampler._expand_to_beam_size = _expand_to_beam_size
from gluonnlp.model.sequence_sampler import SequenceSampler


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Sampling by pretrained GPT-2 model.')
    parser.add_argument('--model', help='The specific model we need to convert', type=str, choices=['117M', '345M'])
    parser.add_argument('--unconditional', action='store_true',
                        help='Whether to sample in the unconditional mode.')
    parser.add_argument('--num', type=int, default=5, help='The number of sentences to sample.')
    parser.add_argument('--ctx', default='gpu0', type=str, help='The context to run the sampling demo.')
    args = parser.parse_args()
    ctx = parse_ctx(args.ctx)[0]
    model, vocab, tokenizer, detokenizer = load_pretrained_GPT2(args.model, ctx=ctx)
    model.hybridize()
    decoder = GPT2Decoder(model)
    eos_id = vocab[vocab.eos_token]
    if args.unconditional:
        sampler = SequenceSampler(beam_size=args.num, max_length=1024, eos_id=eos_id, decoder=decoder)
        unconditional_inputs = mx.nd.array([eos_id], dtype=np.int32, ctx=ctx)
        samples, scores, valid_length = sampler(unconditional_inputs, None)
        samples = samples.asnumpy()
        valid_length = valid_length.asnumpy()
        for i in range(args.num):
            print('-------- Begin Sample {} ---------'.format(i))
            generated_string = detokenizer([vocab.idx_to_token[ele] for ele in samples[0, i, :valid_length[0, i]]])
            print(generated_string)
            print('-------- End Sample {} ---------'.format(i))
    else:
        print('Please type in the start of the sentence, e.g., Machine Learning')
        context = input('Type in the start of the sentence >>> ')
        if not context.startswith(' '):
            context = ' ' + context
        initial_tokens = mx.nd.array([vocab[tokenizer(context)]], dtype=np.int32, ctx=ctx)
        cond_init_input = initial_tokens[:, -1]
        cond_init_states = None
        if initial_tokens.shape[1] > 1:
            _, cond_init_states = model(initial_tokens[:, :-1], None)
        sampler = SequenceSampler(beam_size=args.num, max_length=1024 - initial_tokens.shape[1], eos_id=eos_id, decoder=decoder)
        samples, scores, valid_length = sampler(cond_init_input, None)
        for i in range(args.num):
            print('-------- Begin Sample {} ---------'.format(i))
            generated_string = detokenizer([vocab.idx_to_token[ele] for ele in samples[0, i, :valid_length[0, i]]])
            if initial_tokens.shape[1] > 1:
                generated_string = detokenizer(vocab.idx_to_token[ele] for ele in initial_tokens.asnumpy()[0, :-1])\
                                   + generated_string
            print(generated_string)
            print('-------- End Sample {} ---------'.format(i))


