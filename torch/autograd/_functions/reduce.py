from functools import reduce

from ..function import Function
from ..variable import Variable
import torch


class Sum(Function):

    @staticmethod
    def forward(ctx, input, dim=None, keepdim=False):
        ctx.dim = dim
        ctx.keepdim = keepdim
        ctx.input_size = input.size()
        if dim is None:
            return input.new((input.sum(),))
        else:
            return input.sum(dim, keepdim)

    @staticmethod
    def backward(ctx, grad_output):
        if ctx.dim is None:
            return grad_output.expand(ctx.input_size), None, None
        else:
            if ctx.keepdim is False and len(ctx.input_size) != 1:
                grad_output = grad_output.unsqueeze(ctx.dim)

            repeats = [1 for _ in ctx.input_size]
            repeats[ctx.dim] = ctx.input_size[ctx.dim]
            return grad_output.repeat(*repeats), None, None


class Prod(Function):

    @staticmethod
    def forward(ctx, input, dim=None, keepdim=False):
        ctx.dim = dim
        ctx.keepdim = keepdim
        ctx.input_size = input.size()
        if dim is None:
            ctx.result = input.prod()
            ctx.save_for_backward(input)
            return input.new((ctx.result,))
        else:
            output = input.prod(dim, keepdim)
            ctx.save_for_backward(input, output)
            return output

    @staticmethod
    def backward(ctx, grad_output):
        def safe_zeros_backward(inp, dim):
            # note that the gradient is equivalent to:
            # cumprod(exclusive, normal) * cumprod(exclusive, reverse), e.g.:
            # input:                        [    a,     b,     c]
            # cumprod(exclusive, normal):   [1    ,     a, a * b]
            # cumprod(exclusive, reverse):  [b * c,     c,     1]
            # product:                      [b * c, a * c, a * b]
            # and this is safe under input with 0s.
            if inp.size(dim) == 1:
                return grad_output

            ones_size = torch.Size((inp.size()[:dim] + (1,) + inp.size()[dim + 1:]))
            ones = Variable(grad_output.data.new(ones_size).fill_(1))
            exclusive_normal_nocp = torch.cat((ones, inp.narrow(dim, 0, inp.size(dim) - 1)), dim)
            exclusive_normal = exclusive_normal_nocp.cumprod(dim)

            def reverse_dim(var, dim):
                return var.index_select(dim, Variable(torch.arange(var.size(dim) - 1, -1, -1)).long())

            narrow_reverse = reverse_dim(inp.narrow(dim, 1, inp.size(dim) - 1), dim)
            exclusive_reverse_nocp = torch.cat((ones, narrow_reverse), dim)
            exclusive_reverse = reverse_dim(exclusive_reverse_nocp.cumprod(dim), dim)

            grad_input = grad_output.expand_as(exclusive_normal).mul(exclusive_normal.mul(exclusive_reverse))
            return grad_input

        if ctx.dim is None:
            input, = ctx.saved_variables
            zero_idx = (input.data == 0).nonzero()
            if zero_idx.dim() == 0:
                return grad_output.mul(ctx.result).expand_as(input).div(input), None, None
            elif zero_idx.size(0) > 1:
                return (grad_output * 0).expand_as(input), None, None
            else:
                return safe_zeros_backward(input.view(-1), 0).view_as(input), None, None

        else:
            input, output = ctx.saved_variables
            dim = ctx.dim if ctx.dim >= 0 else ctx.dim + input.dim()
            if ctx.keepdim is False and len(ctx.input_size) != 1:
                grad_output = grad_output.unsqueeze(dim)
                output = output.unsqueeze(dim)

            zero_mask = input == 0
            slice_zero_count = zero_mask.sum(dim, True)
            total_zeros = slice_zero_count.data.sum()
            if total_zeros == 0:
                grad_input = grad_output.mul(output).expand_as(input).div(input)
            else:
                grad_input = safe_zeros_backward(input, dim)

            return grad_input, None, None


class Mean(Function):

    @staticmethod
    def forward(ctx, input, dim=None, keepdim=False):
        ctx.dim = dim
        ctx.keepdim = keepdim
        ctx.input_size = input.size()
        if dim is None:
            return input.new((input.mean(),))
        else:
            return input.mean(dim, keepdim)

    @staticmethod
    def backward(ctx, grad_output):
        if ctx.dim is None:
            grad_input_val = grad_output / reduce(lambda x, y: x * y, ctx.input_size, 1)
            return grad_input_val.expand(ctx.input_size), None, None
        else:
            if ctx.keepdim is False and len(ctx.input_size) != 1:
                grad_output = grad_output.unsqueeze(ctx.dim)

            repeats = [1 for _ in ctx.input_size]
            dim_size = ctx.input_size[ctx.dim]
            repeats[ctx.dim] = dim_size
            return grad_output.repeat(*repeats).div_(dim_size), None, None


class _SelectionFunction(Function):
    has_all_reduce = True
    # additional_args is prepended before dim when calling the tensor
    # function. It's a no-op for subclasses other than kthvalue.
    # kthvalue not only requires us to pass a dim, but also preceed it with k.

    @classmethod
    def forward(cls, ctx, input, dim=None, keepdim=False, additional_args=tuple()):
        fn = getattr(input, cls.__name__.lower())
        ctx.dim = dim
        ctx.keepdim = keepdim
        ctx.additional_args = additional_args
        ctx.input_size = input.size()
        if ctx.dim is None and cls.has_all_reduce:
            value = fn(*additional_args)
            ctx.indices_tuple = tuple(input.eq(value).nonzero()[0])
            return input.new((value,))
        else:
            if ctx.dim is None:
                dim = input.dim() - 1
            else:
                dim = ctx.dim
            args = (dim, keepdim)
            if additional_args:
                args = additional_args + args
            output, indices = fn(*args)
            ctx.save_for_backward(indices)
            ctx.mark_non_differentiable(indices)
            return output, indices

    @classmethod
    def backward(cls, ctx, grad_output, grad_indices=None):
        grad_input = Variable(grad_output.data.new(*ctx.input_size).zero_())
        if ctx.dim is None and cls.has_all_reduce:
            grad_input[ctx.indices_tuple] = grad_output
        else:
            if ctx.dim is None:
                dim = len(ctx.input_size) - 1
            else:
                dim = ctx.dim

            indices, = ctx.saved_variables
            if ctx.keepdim is False and len(ctx.input_size) != 1:
                grad_output = grad_output.unsqueeze(dim)
                grad_indices = grad_indices.unsqueeze(dim)
                indices = indices.unsqueeze(dim)

            grad_input.scatter_(dim, indices, grad_output)
        return grad_input, None, None, None


class Max(_SelectionFunction):
    pass


class Min(_SelectionFunction):
    pass


class Mode(_SelectionFunction):
    has_all_reduce = False


class Median(_SelectionFunction):
    has_all_reduce = False


class Kthvalue(_SelectionFunction):
    has_all_reduce = False

    @classmethod
    def forward(cls, ctx, input, k, dim=None, keepdim=False):
        return super(Kthvalue, cls).forward(ctx, input, dim, keepdim, (k,))


class Norm(Function):

    @staticmethod
    def forward(ctx, input, p=2, dim=None, keepdim=False):
        ctx.p = p
        ctx.dim = dim
        ctx.keepdim = keepdim

        if dim is None:
            ctx.norm = input.norm(p)
            ctx.save_for_backward(input)
            return input.new((ctx.norm,))
        else:
            output = input.norm(p, dim, keepdim)
            ctx.save_for_backward(input, output)
            return output

    @staticmethod
    def backward(ctx, grad_output):
        if ctx.dim is None:
            input, = ctx.saved_variables
            if ctx.p == 2:
                scale_v = (grad_output / ctx.norm).expand_as(input)
                return input.mul(scale_v), None, None, None
            else:
                pow = input.abs().pow(ctx.p - 2)
                scale_v = (grad_output / ctx.norm ** (ctx.p - 1)).expand_as(input)
                return input.mul(pow).mul(scale_v), None, None, None
        else:
            input, output = ctx.saved_variables

            if ctx.keepdim is False and input.dim() != 1:
                grad_output = grad_output.unsqueeze(ctx.dim)
                output = output.unsqueeze(ctx.dim)

            big_grad_output = grad_output.expand_as(input)
            if ctx.p == 2:
                big_output = output.expand_as(input)
                return input.mul(big_grad_output).div(big_output), None, None, None
            else:
                pow = input.abs().pow(ctx.p - 2)
                big_output = output.pow(ctx.p - 1).expand_as(input)
                return input.mul(pow).mul(big_grad_output).div(big_output), None, None, None


# TODO: renorm
# TODO: std
# TODO: var
