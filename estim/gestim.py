import torch
import torch.nn
import torch.multiprocessing
import numpy as np
import math
import random

import copy
import logging

from data import InfiniteLoader


class GradientEstimator(object):
    def __init__(self, data_loader, opt, tb_logger=None, *args, **kwargs):
        self.opt = opt
        self.model = None
        self.data_loader = data_loader
        self.tb_logger = tb_logger
        self.niters = 0
        self.random_indices = None

    def update_niters(self, niters):
        self.niters = niters

    def init_data_iter(self):
        self.data_iter = iter(InfiniteLoader(self.data_loader))
        self.estim_iter = iter(InfiniteLoader(self.data_loader))

    def snap_batch(self, model):
        pass

    def update_sampler(self):
        pass

    def _calc_stats_buckets(self, buckets):
        stats = {
            'sigma': [],
            'mean': []
        }
        i = 0
        for bucket in buckets:
            current_bk = torch.stack(buckets[bucket])
            stats['mean'].append(torch.mean(current_bk).cpu().item())
            stats['sigma'].append(torch.sqrt(torch.mean(
                torch.var(current_bk, dim=0, unbiased=False))).cpu().item())
            i += 1

        return stats
    
    def _get_grad_samples(self, model, num_of_samples):
        grads = []
        for i in range(num_of_samples):
            grad = self.grad_estim(model)
            copy_array = []
            for layer in grad:
                copy_array.append(layer.clone())

            grads.append(copy_array)
        return grads

    def _get_stats_lb(self, grads):
        # get stats layer based
        bs = self.opt.nuq_bucket_size
        nuq_layer = self.opt.nuq_layer
        sep_bias_grad = self.opt.sep_bias_grad

        # total number of weights
        nw = sum([w.numel() for w in grads[0]])

        # total sum of gradients
        tsum = torch.zeros(nw).cuda()

        buckets = None
        total_norm = None
        for i, grad in enumerate(grads):
            fl_norm_lb = self._flatt_and_normalize_lb(grad, bs, nocat=True)
            if buckets is None:
                buckets = [[] for j in range(len(fl_norm_lb))]
                total_norm = [0.0 for j in range(len(fl_norm_lb))]

            fl_norm = self._flatten_lb(grad, nocat=True)
            tsum += self._flatten_lb(fl_norm_lb, nocat=False)
            for j in range(len(fl_norm_lb)):
                buckets[j].append(fl_norm_lb[j])
                total_norm[j] += fl_norm[j].norm()

        stats = self._calc_stats_buckets(buckets)
        stats['norm'] = torch.tensor(total_norm)

        return stats

    def _get_stats_lb_sep(self, grads):
        # get stats layer based
        bs = self.opt.nuq_bucket_size
        nuq_layer = self.opt.nuq_layer
        sep_bias_grad = self.opt.sep_bias_grad

        # total number of weights
        nw = sum([w.numel() for w in grads[0]])

        # total sum of gradients
        tsum_bias = 0.0
        tsum_weights = 0.0

        buckets_bias = {}
        total_norm_bias = {}

        buckets_weights = {}
        total_norm_weights = {}

        fl_norm_bias, fl_norm_weights = self._flatten_sep(grads[0])
        fl_norm_lb_bias, fl_norm_lb_weights = \
            self._flatt_and_normalize_lb_sep(grads[0], bs, nocat=True)

        j = 0
        for layer in fl_norm_lb_bias:
            for bias in layer:
                buckets_bias[j] = []
                total_norm_bias[j] = 0.0
                j += 1

        j = 0
        for layer in fl_norm_lb_weights:
            for weights in layer:
                buckets_weights[j] = []
                total_norm_weights[j] = 0.0
                j += 1

        for i, grad in enumerate(grads):
            fl_norm_lb_bias, fl_norm_lb_weights = \
                self._flatt_and_normalize_lb_sep(grad, bs, nocat=True)

            fl_norm_bias, fl_norm_weights = self._flatten_lb_sep(grad, bs)
            tsum_bias += self._flatten([torch.cat(layer)
                                        for layer in fl_norm_lb_bias])
            tsum_weights += self._flatten([torch.cat(layer)
                                           for layer in fl_norm_lb_weights])

            j = 0
            for layer in fl_norm_lb_bias:
                for bias in layer:
                    buckets_bias[j].append(bias)
                    j += 1

            j = 0
            for layer in fl_norm_lb_weights:
                for weight in layer:
                    buckets_weights[j].append(weight)
                    j += 1

            j = 0
            for layer in fl_norm_bias:
                for bias in layer:
                    total_norm_bias[j] += bias.norm()
                    j += 1

            j = 0
            for layer in fl_norm_weights:
                for weight in layer:
                    total_norm_weights[j] += weight.norm()
                    j += 1

        stats_bias = self._calc_stats_buckets(buckets_bias)
        stats_bias['norm'] = torch.tensor(list(total_norm_bias.values()))
        stats_bias['norm'] = stats_bias['norm'].cpu().tolist()

        stats_weights = self._calc_stats_buckets(buckets_weights)
        stats_weights['norm'] = torch.tensor(list(total_norm_weights.values()))
        stats_weights['norm'] = stats_weights['norm'].cpu().tolist()

        return stats_bias, stats_weights

    def _get_stats_sep(self, grads):
        # get stats for weights and bias separately
        pass

    def _get_stats(self, grads):
        # get stats
        bucket_size = self.opt.nuq_bucket_size
        nuq_layer = self.opt.nuq_layer
        sep_bias_grad = self.opt.sep_bias_grad

        for grad in grads:
            flattened, flattened_lb = self.flatten_and_normalize(
                grad, bucket_size, nocat=True)

            flattened_lb_flt, _ = self.flatten(flattened_lb, nocat=True)
            flatt_unnorm, flattened_unnormalized_lb = self.flatten(grad)
            with torch.no_grad():
                if self.opt.nuq_layer == 0:
                    tsum += flattened_lb_flt
                else:
                    tsum += flattened
            if self.opt.nuq_layer == 1:
                flatt_unnorm, _ = self.flatten(grad)
                num_bucket = int(np.ceil(len(flattened) / bucket_size))
                for bucket_i in range(num_bucket):
                    start = bucket_i * bucket_size
                    end = min((bucket_i + 1) * bucket_size, len(flattened))
                    x_bucket = flattened[start:end].clone()
                    x_bucket_unnormalized = flatt_unnorm[start:end].clone()
                    if bucket_i not in buckets_normalized.keys():
                        buckets_normalized[bucket_i] = []
                        buckets[bucket_i] = 0

                    buckets_normalized[bucket_i].append(x_bucket)
                    buckets[bucket_i] += x_bucket_unnormalized.norm()
            else:
                bucket_index = 0
                for layer, flattened in enumerate(flattened_lb):
                    num_bucket = int(np.ceil(len(flattened) / bucket_size))
                    for bucket_i in range(num_bucket):
                        start = bucket_i * bucket_size
                        end = min((bucket_i + 1) * bucket_size, len(flattened))
                        x_bucket = flattened[start:end].clone()
                        x_bucket_unnormalized = flattened_unnormalized_lb[layer][start:end].clone(
                        )
                        if i == 0:
                            buckets_normalized[bucket_index] = []
                            buckets[bucket_index] = 0

                        buckets_normalized[bucket_index].append(x_bucket)
                        buckets[bucket_index] += x_bucket_unnormalized.norm()
                        bucket_index += 1
        mean = tsum / num_of_samples
        total_mean = torch.sum(mean) / (nw)

        for i in range(num_of_samples):
            grad = grads[i]
            flattened, flattened_lb = self.flatten_and_normalize(
                grad, bucket_size)

            flattened_lb_flt, _ = self.flatten(flattened_lb)

            with torch.no_grad():
                if self.opt.nuq_layer == 0:
                    total_variance += (flattened_lb_flt - mean).pow(2).sum()
                else:
                    total_variance += (flattened - mean).pow(2).sum()

        norm_results = {
            'norm': [],
            'sigma': [],
            'mean': []
        }

        i = 0
        for bucket in buckets_normalized:
            current_bk = torch.stack(buckets_normalized[bucket])
            norm_results['mean'].append(torch.mean(current_bk).cpu().item())
            norm_results['sigma'].append(torch.sqrt(torch.mean(
                torch.var(current_bk, dim=0, unbiased=False))).cpu().item())
            norm_results['norm'].append(
                (buckets[i] / num_of_samples).cpu().item())
            i += 1

        # if len(norm_results['mean']) > self.opt.dist_num:
        #     indexes = np.argsort(-np.asarray(norm_results['norm']))[:self.opt.dist_num]
        #     norm_results['mean'] = np.array(norm_results['mean'])[indexes].tolist()
        #     norm_results['sigma'] = np.array(norm_results['sigma'])[indexes].tolist()
        #     norm_results['norm'] = np.array(norm_results['norm'])[indexes].tolist()
        total_variance /= (num_of_samples * nw)
        print(norm_results)

        return total_mean, total_variance, norm_results

    def snap_online(self, model):
        num_of_samples = self.opt.nuq_number_of_samples
        grads = self._get_grad_samples(model, num_of_samples)
        return self._get_stats_lb_sep(grads)
        # total_variance = 0
        # num_of_samples = self.opt.nuq_number_of_samples
        # sep_bias_grad = self.opt.sep_bias_grad

        # # total number of weights
        # nw = sum([w.numel() for w in model.parameters()])

        # # total sum of weights
        # tsum = torch.zeros(nw).cuda()

        # grads = self._get_grad_samples(num_of_samples)

        # if sep_bias_grad:
        #     return self._get_stats_sep(grads)
        # else:
        #     return self._get_stats(grads)

        # buckets_normalized = {}
        # buckets = {}
        # for i in range(num_of_samples):
        #     grad = grads[i]
        #     if sep_bias_grad:
        #         flattened, flattened_lb = self.flatten_and_normalize_sep(
        #             grad, bucket_size, nocat=True)
        #     else:
        #         flattened, flattened_lb = self.flatten_and_normalize(
        #             grad, bucket_size, nocat=True)

        #     flattened_lb_flt, _ = self.flatten(flattened_lb)
        #     flatt_unnorm, flattened_unnormalized_lb = self.flatten(
        #         grad)
        #     with torch.no_grad():
        #         if self.opt.nuq_layer == 0:
        #             tsum += flattened_lb_flt
        #         else:
        #             tsum += flattened
        #     if self.opt.nuq_layer == 1:
        #         flatt_unnorm, _ = self.flatten(grad)
        #         num_bucket = int(np.ceil(len(flattened) / bucket_size))
        #         for bucket_i in range(num_bucket):
        #             start = bucket_i * bucket_size
        #             end = min((bucket_i + 1) * bucket_size, len(flattened))
        #             x_bucket = flattened[start:end].clone()
        #             x_bucket_unnormalized = flatt_unnorm[start:end].clone()
        #             if bucket_i not in buckets_normalized.keys():
        #                 buckets_normalized[bucket_i] = []
        #                 buckets[bucket_i] = 0

        #             buckets_normalized[bucket_i].append(x_bucket)
        #             buckets[bucket_i] += x_bucket_unnormalized.norm()
        #     else:
        #         bucket_index = 0
        #         for layer, flattened in enumerate(flattened_lb):
        #             num_bucket = int(np.ceil(len(flattened) / bucket_size))
        #             for bucket_i in range(num_bucket):
        #                 start = bucket_i * bucket_size
        #                 end = min((bucket_i + 1) * bucket_size, len(flattened))
        #                 x_bucket = flattened[start:end].clone()
        #                 x_bucket_unnormalized = flattened_unnormalized_lb[layer][start:end].clone(
        #                 )
        #                 if i == 0:
        #                     buckets_normalized[bucket_index] = []
        #                     buckets[bucket_index] = 0

        #                 buckets_normalized[bucket_index].append(x_bucket)
        #                 buckets[bucket_index] += x_bucket_unnormalized.norm()
        #                 bucket_index += 1
        # mean = tsum / num_of_samples
        # total_mean = torch.sum(mean) / (nw)

        # for i in range(num_of_samples):
        #     grad = grads[i]
        #     flattened, flattened_lb = self.flatten_and_normalize(
        #         grad, bucket_size)

        #     flattened_lb_flt, _ = self.flatten(flattened_lb)

        #     with torch.no_grad():
        #         if self.opt.nuq_layer == 0:
        #             total_variance += (flattened_lb_flt - mean).pow(2).sum()
        #         else:
        #             total_variance += (flattened - mean).pow(2).sum()

        # norm_results = {
        #     'norm': [],
        #     'sigma': [],
        #     'mean': []
        # }

        # i = 0
        # for bucket in buckets_normalized:
        #     current_bk = torch.stack(buckets_normalized[bucket])
        #     norm_results['mean'].append(torch.mean(current_bk).cpu().item())
        #     norm_results['sigma'].append(torch.sqrt(torch.mean(
        #         torch.var(current_bk, dim=0, unbiased=False))).cpu().item())
        #     norm_results['norm'].append(
        #         (buckets[i] / num_of_samples).cpu().item())
        #     i += 1

        # # if len(norm_results['mean']) > self.opt.dist_num:
        # #     indexes = np.argsort(-np.asarray(norm_results['norm']))[:self.opt.dist_num]
        # #     norm_results['mean'] = np.array(norm_results['mean'])[indexes].tolist()
        # #     norm_results['sigma'] = np.array(norm_results['sigma'])[indexes].tolist()
        # #     norm_results['norm'] = np.array(norm_results['norm'])[indexes].tolist()
        # total_variance /= (num_of_samples * nw)
        # print(norm_results)

        # return total_mean, total_variance, norm_results

        # return torch.tensor(0), torch.tensor(0.01), norm_results

    def grad(self, model_new, in_place=False, data=None):
        raise NotImplementedError('grad not implemented')

    def _normalize(self, layer, bucket_size, nocat=False):
        normalized = []
        num_bucket = int(np.ceil(len(layer) / bucket_size))
        for bucket_i in range(num_bucket):
            start = bucket_i * bucket_size
            end = min((bucket_i + 1) * bucket_size, len(layer))
            x_bucket = layer[start:end].clone()
            norm = x_bucket.norm()
            normalized.append(x_bucket / (norm + 1e-7))
        if not nocat:
            return torch.cat(normalized)
        else:
            return normalized

    def grad_estim(self, model):
        # ensuring continuity of data seen in training
        # TODO: make sure sub-classes never use any other data_iter, e.g. raw
        dt = self.data_iter
        self.data_iter = self.estim_iter
        ret = self.grad(model)
        self.data_iter = dt
        return ret

    def get_Ege_var(self, model, gviter):
        # estimate grad mean and variance
        Ege = [torch.zeros_like(g) for g in model.parameters()]
        for i in range(gviter):
            ge = self.grad_estim(model)
            for e, g in zip(Ege, ge):
                e += g

        for e in Ege:
            e /= gviter

        nw = sum([w.numel() for w in model.parameters()])
        var_e = 0
        Es = [torch.zeros_like(g) for g in model.parameters()]
        En = [torch.zeros_like(g) for g in model.parameters()]
        for i in range(gviter):
            ge = self.grad_estim(model)
            v = sum([(gg-ee).pow(2).sum() for ee, gg in zip(Ege, ge)])
            for s, e, g, n in zip(Es, Ege, ge, En):
                s += g.pow(2)
                n += (e-g).pow(2)
            var_e += v/nw

        var_e /= gviter
        # Division by gviter cancels out in ss/nn
        snr_e = sum(
            [((ss+1e-10).log()-(nn+1e-10).log()).sum()
             for ss, nn in zip(Es, En)])/nw
        nv_e = sum([(nn/(ss+1e-7)).sum() for ss, nn in zip(Es, En)])/nw
        return Ege, var_e, snr_e, nv_e

    def _flatten_lb_sep(self, gradient, bs=None):
        # flatten layer based and handle weights and bias separately
        flatt_params = [], []

        for layer in gradient:
            if len(layer.size()) == 1:
                if bs is None:
                    flatt_params[0].append(
                        torch.flatten(layer))
                else:
                    buckets = []
                    flatt = torch.flatten(layer)
                    num_bucket = int(np.ceil(len(flatt) / bs))
                    for bucket_i in range(num_bucket):
                        start = bucket_i * bs
                        end = min((bucket_i + 1) * bs, len(flatt))
                        x_bucket = flatt[start:end].clone()
                        buckets.append(x_bucket)
                    flatt_params[0].append(
                        buckets)
            else:
                if bs is None:
                    flatt_params[1].append(
                        torch.flatten(layer))
                else:
                    buckets = []
                    flatt = torch.flatten(layer)
                    num_bucket = int(np.ceil(len(flatt) / bs))
                    for bucket_i in range(num_bucket):
                        start = bucket_i * bs
                        end = min((bucket_i + 1) * bs, len(flatt))
                        x_bucket = flatt[start:end].clone()
                        buckets.append(x_bucket)
                    flatt_params[1].append(
                        buckets)
        return flatt_params

    def _flatten_lb(self, gradient):
        # flatten layer based
        flatt_params = []

        for layer_parameters in gradient:
            flatt_params.append(torch.flatten(layer_parameters))

        return flatt_params

    def _flatten_sep(self, gradient, bs=None):
        # flatten weights and bias separately
        flatt_params = [], []

        for layer_parameters in gradient:
            if len(layer_parameters.size()) == 1:
                flatt_params[0].append(
                    torch.flatten(layer_parameters))
            else:
                flatt_params[1].append(torch.flatten(layer_parameters))
        return torch.cat(flatt_params[0]), torch.cat(flatt_params[1])

    def _flatten(self, gradient):
        flatt_params = []
        for layer_parameters in gradient:
            flatt_params.append(torch.flatten(layer_parameters))

        return torch.cat(flatt_params)

    def unflatten(self, gradient, parameters):
        shaped_gradient = []
        begin = 0
        for layer in parameters:
            size = layer.view(-1).shape[0]
            shaped_gradient.append(
                gradient[begin:begin+size].view(layer.shape))
            begin += size
        return shaped_gradient

    def _flatt_and_normalize_lb_sep(self, gradient, bucket_size=1024,
                                    nocat=False):
        # flatten and normalize weight and bias separately

        bs = bucket_size
        # totally flat and layer-based layers
        flatt_params_lb = self._flatten_lb_sep(gradient)

        normalized_buckets_lb = [], []

        for bias in flatt_params_lb[0]:
            normalized_buckets_lb[0].append(
                self._normalize(bias, bucket_size, nocat))
        for weight in flatt_params_lb[1]:
            normalized_buckets_lb[1].append(
                self._normalize(weight, bucket_size, nocat))
        return normalized_buckets_lb

    def _flatt_and_normalize_lb(self, gradient, bucket_size=1024, nocat=False):
        flatt_params_lb = self._flatten_lb(gradient)

        normalized_buckets_lb = []
        for layer in flatt_params_lb:
            normalized_buckets_lb.append(
                self._normalize(layer, bucket_size, nocat))
        return normalized_buckets_lb

    def _flatt_and_normalize(self, gradient, bucket_size=1024, nocat=False):
        flatt_params = self._flatten(gradient)

        return self._normalize(flatt_params, bucket_size, nocat)

    def _flatt_and_normalize_sep(self, gradient,
                                 bucket_size=1024, nocat=False):
        flatt_params = self._flatten_sep(gradient)

        return [self._normalize(flatt_params[0], bucket_size, nocat),
                self._normalize(flatt_params[1], bucket_size, nocat)]

    def get_gradient_distribution(self, model, gviter, bucket_size):
        """
        gviter: Number of minibatches to apply on the model
        model: Model to be evaluated
        """
        bucket_size = self.opt.nuq_bucket_size
        mean_estimates_normalized, mean_estimates_unconcatenated = self.flatten_and_normalize(
            model.parameters(), bucket_size)
        # estimate grad mean and variance
        mean_estimates = [torch.zeros_like(g) for g in model.parameters()]
        mean_estimates_unconcatenated = [torch.zeros_like(
            g) for g in mean_estimates_unconcatenated]
        mean_estimates_normalized = torch.zeros_like(mean_estimates_normalized)

        for i in range(gviter):
            minibatch_gradient = self.grad_estim(model)
            minibatch_gradient_normalized, minibatch_gradient_unconcatenated = self.flatten_and_normalize(
                minibatch_gradient, bucket_size)

            for e, g in zip(mean_estimates, minibatch_gradient):
                e += g

            for e, g in zip(mean_estimates_unconcatenated, minibatch_gradient_unconcatenated):
                e += g

            mean_estimates_normalized += minibatch_gradient_normalized

        # Calculate the mean
        for e in mean_estimates:
            e /= gviter

        for e in mean_estimates_unconcatenated:
            e /= gviter

        mean_estimates_normalized /= gviter

        # Number of Weights
        number_of_weights = sum([layer.numel()
                                 for layer in model.parameters()])

        variance_estimates = [torch.zeros_like(g) for g in model.parameters()]
        variance_estimates_unconcatenated = [
            torch.zeros_like(g) for g in mean_estimates_unconcatenated]

        variance_estimates_normalized = torch.zeros_like(
            mean_estimates_normalized)

        for i in range(gviter):
            minibatch_gradient = self.grad_estim(model)
            minibatch_gradient_normalized, minibatch_gradient_unconcatenated = self.flatten_and_normalize(
                minibatch_gradient, bucket_size)

            v = [(gg - ee).pow(2)
                 for ee, gg in zip(mean_estimates, minibatch_gradient)]
            v_normalized = (mean_estimates_normalized -
                            minibatch_gradient_normalized).pow(2)
            v_normalized_unconcatenated = [(gg - ee).pow(2) for ee, gg in zip(
                mean_estimates_unconcatenated, minibatch_gradient_unconcatenated)]
            for e, g in zip(variance_estimates, v):
                e += g

            for e, g in zip(variance_estimates_unconcatenated, v_normalized_unconcatenated):
                e += g

            variance_estimates_normalized += v_normalized

        variance_estimates_normalized = variance_estimates_normalized / gviter
        for e in variance_estimates_unconcatenated:
            e /= gviter

        variances = []
        means = []
        # random_indices = self.get_random_index(model, 4)
        # for index in random_indices:
        #     variance_estimate_layer = variance_estimates[index[0]]
        #     mean_estimate_layer = mean_estimates[index[0]]

        #     for weight in index[1:]:
        #         variance_estimate_layer = variance_estimate_layer[weight]
        #         variance_estimate_layer.squeeze_()

        #         mean_estimate_layer = mean_estimate_layer[weight]
        #         mean_estimate_layer.squeeze_()
        #     variance = variance_estimate_layer / (gviter)

        #     variances.append(variance)
        #     means.append(mean_estimate_layer)

        total_mean = torch.tensor(0, dtype=float)
        for mean_estimate in mean_estimates:
            total_mean += torch.sum(mean_estimate)

        total_variance = torch.tensor(0, dtype=float)
        for variance_estimate in variance_estimates:
            total_variance += torch.sum(variance_estimate)

        total_variance = total_variance / number_of_weights
        total_mean = total_mean / number_of_weights

        total_variance_normalized = torch.tensor(0, dtype=float)
        total_variance_normalized = torch.sum(
            variance_estimates_normalized) / number_of_weights
        total_mean_normalized = torch.tensor(0, dtype=float)
        total_mean_normalized = torch.sum(
            mean_estimates_normalized) / number_of_weights
        total_mean_unconcatenated = sum([torch.sum(
            mean) / mean.numel() for mean in mean_estimates_unconcatenated]) / len(mean_estimates)
        total_variance_unconcatenated = sum([torch.sum(variance) / variance.numel(
        ) for variance in variance_estimates_unconcatenated]) / len(mean_estimates)

        return variances, means, total_mean, total_variance, total_variance_normalized, total_mean_normalized, total_mean_unconcatenated, total_variance_unconcatenated

    def get_norm_distribution(self, model, gviter, bucket_size=1024):
        norms = {}
        for i in range(gviter):
            minibatch_gradient = self.grad_estim(model)
            flattened_parameters, less_flattened = self.flatten(
                minibatch_gradient)
            num_bucket = int(np.ceil(len(flattened_parameters) / bucket_size))
            for bucket_i in range(num_bucket):
                start = bucket_i * bucket_size
                end = min((bucket_i + 1) * bucket_size,
                          len(flattened_parameters))
                if (end == len(flattened_parameters)):
                    continue
                x_bucket = flattened_parameters[start:end].clone()
                norm = x_bucket.norm()
                if norm.cpu() in norms.keys():
                    print('An error occured')
                norms[norm.cpu()] = x_bucket
        return norms

    def state_dict(self):
        return {}

    def load_state_dict(self, state):
        pass

    def snap_model(self, model):
        logging.info('Snap Model')
        if self.model is None:
            self.model = copy.deepcopy(model)
            return
        # update sum
        for m, s in zip(model.parameters(), self.model.parameters()):
            s.data.copy_(m.data)
