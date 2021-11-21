"""Implement user code."""

import torch
import copy


class UserSingleStep(torch.nn.Module):
    """A user who computes a single local update step."""

    def __init__(self, model, loss, dataloader, setup, cfg_user, num_queries=1):
        """Initialize from cfg_user dict which contains atleast all keys in the matching .yaml :>"""
        super().__init__()
        self.num_data_points = cfg_user.num_data_points
        self.num_user_queries = num_queries

        self.provide_labels = cfg_user.provide_labels
        self.provide_num_data_points = cfg_user.provide_num_data_points

        if cfg_user.data_idx is None:
            self.data_idx = torch.randint(0, len(dataloader.dataset), (1,))
        else:
            self.data_idx = cfg_user.data_idx
        self.data_with_labels = cfg_user.data_with_labels

        self.setup = setup

        self.model = copy.deepcopy(model)
        self.model.to(**setup)
        if cfg_user.batch_norm_training:
            self.model.training()
        else:
            self.model.eval()

        self._initialize_local_privacy_measures(cfg_user.local_diff_privacy)

        self.dataloader = dataloader
        self.loss = copy.deepcopy(loss)  # Just in case the loss contains state

    def __repr__(self):
        return f"""User (of type {self.__class__.__name__} with settings:
            number of data points: {self.num_data_points}
            number of user queries {self.num_user_queries}

            Threat model:
            User provides labels: {self.provide_labels}
            User provides number of data points: {self.provide_num_data_points}

            Model:
            model specification: {str(self.model.__class__.__name__)}
            loss function: {str(self.loss)}

            Data:
            Dataset: {self.dataloader.dataset.__class__.__name__}
            data_idx: {self.data_idx.item() if isinstance(self.data_idx, torch.Tensor) else self.data_idx}
        """

    def _initialize_local_privacy_measures(self, local_diff_privacy):
        """Initialize generators for noise in either gradient or input."""
        if local_diff_privacy['gradient_noise'] > 0.0:
            loc = torch.as_tensor(0.0, **self.setup)
            scale = torch.as_tensor(local_diff_privacy['gradient_noise'], **self.setup)
            if local_diff_privacy['distribution'] == 'gaussian':
                self.generator = torch.distributions.normal.Normal(loc=loc, scale=scale)
            elif local_diff_privacy['distribution'] == 'laplacian':
                self.generator = torch.distributions.laplace.Laplace(loc=loc, scale=scale)
            else:
                raise ValueError(f'Invalid distribution {local_diff_privacy["distribution"]} given.')
        else:
            self.generator = None
        if local_diff_privacy['input_noise'] > 0.0:
            loc = torch.as_tensor(0.0, **self.setup)
            scale = torch.as_tensor(local_diff_privacy['input_noise'], **self.setup)
            if local_diff_privacy['distribution'] == 'gaussian':
                self.generator_input = torch.distributions.normal.Normal(loc=loc, scale=scale)
            elif local_diff_privacy['distribution'] == 'laplacian':
                self.generator_input = torch.distributions.laplace.Laplace(loc=loc, scale=scale)
            else:
                raise ValueError(f'Invalid distribution {local_diff_privacy["distribution"]} given.')
        else:
            self.generator_input = None
        self.clip_value = local_diff_privacy.get('per_example_clipping', 0.0)

    def compute_local_updates(self, server_payload):
        """Compute local updates to the given model based on server payload."""

        data, labels = self._generate_example_data()
        # Compute local updates
        shared_grads = []
        shared_buffers = []
        for query in range(self.num_user_queries):
            payload = server_payload['queries'][query]
            parameters = payload['parameters']
            buffers = payload['buffers']

            with torch.no_grad():
                for param, server_state in zip(self.model.parameters(), parameters):
                    param.copy_(server_state.to(**self.setup))
                for buffer, server_state in zip(self.model.buffers(), buffers):
                    buffer.copy_(server_state.to(**self.setup))

            def _compute_batch_gradient(data, labels):
                data_input = data + self.generator_input.sample(data.shape) if self.generator_input is not None else data
                outputs = self.model(data_input)
                loss = self.loss(outputs, labels)
                return torch.autograd.grad(loss, self.model.parameters())

            if self.clip_value > 0:  # Compute per-example gradients and clip them in this case
                grads = [torch.zeros_like(p) for p in self.model.parameters()]
                for data_point, data_label in zip(data, labels):
                    per_example_grads = _compute_batch_gradient(data_point[None, ...], data_label[None, ...])
                    self._clip_list_of_grad_(per_example_grads)
                    torch._foreach_add_(grads, per_example_grads)
                torch._foreach_div_(grads, len(data))
            else:
                # Compute the forward pass
                grads = _compute_batch_gradient(data, labels)
            self._apply_differential_noise(grads)
            shared_grads += [grads]
            shared_buffers += [[b.clone().detach() for b in self.model.buffers()]]

        shared_data = dict(gradients=shared_grads, buffers=shared_buffers,
                           num_data_points=self.num_data_points if self.provide_num_data_points else None,
                           labels=labels if self.provide_labels else None,
                           local_hyperparams=None)
        true_user_data = dict(data=data, labels=labels)

        return shared_data, true_user_data

    def _clip_list_of_grad_(self, grads):
        """Apply differential privacy component per-example clipping."""
        grad_norm = torch.norm(torch.stack([torch.norm(g, 2) for g in grads]), 2)
        if grad_norm > self.clip_value:
            [g.mul_(self.clip_value / (grad_norm + 1e-6)) for g in grads]

    def _apply_differential_noise(self, grads):
        """Apply differential privacy component gradient noise."""
        if self.generator is not None:
            for grad in grads:
                grad += self.generator.sample(grad.shape)

    def _generate_example_data(self):
        # Select data
        data = []
        labels = []
        pointer = self.data_idx
        for data_point in range(self.num_data_points):
            datum, label = self.dataloader.dataset[pointer]
            data += [datum]
            labels += [torch.as_tensor(label)]
            if self.data_with_labels == 'unique':
                pointer += len(self.dataloader.dataset) // len(self.dataloader.dataset.classes)
            elif self.data_with_labels == 'same':
                pointer += 1
            elif self.data_with_labels == 'random-animals':  # only makes sense on ImageNet, disregard otherwise
                last_animal_class_idx = 397
                per_class = len(self.dataloader.dataset) // len(self.dataloader.dataset.classes)
                pointer = torch.randint(0, per_class * last_animal_class_idx, (1,))
            else:
                pointer = torch.randint(0, len(self.dataloader.dataset), (1,))
            pointer = pointer % len(self.dataloader.dataset)  # Make sure not to leave the dataset range
        data = torch.stack(data).to(**self.setup)
        labels = torch.stack(labels).to(device=self.setup['device'])
        return data, labels

    def plot(self, user_data, scale=False, print_labels=False):
        """Plot user data to output. Probably best called from a jupyter notebook."""
        import matplotlib.pyplot as plt  # lazily import this here

        dm = torch.as_tensor(self.dataloader.dataset.mean, **self.setup)[None, :, None, None]
        ds = torch.as_tensor(self.dataloader.dataset.std, **self.setup)[None, :, None, None]
        classes = self.dataloader.dataset.classes

        data = user_data['data'].clone().detach()
        labels = user_data['labels'].clone().detach() if user_data['labels'] is not None else None
        if labels is None:
            print_labels = False

        if scale:
            min_val, max_val = data.amin(dim=[2, 3], keepdim=True), data.amax(dim=[2, 3], keepdim=True)
            # print(f'min_val: {min_val} | max_val: {max_val}')
            data = (data - min_val) / (max_val - min_val)
        else:
            data.mul_(ds).add_(dm).clamp_(0, 1)

        if data.shape[0] == 1:
            plt.axis('off')
            plt.imshow(data[0].permute(1, 2, 0).cpu())
            if print_labels:
                plt.title(f'Data with label {classes[labels]}')
        else:
            grid_shape = int(torch.as_tensor(data.shape[0]).sqrt().ceil())
            s = 24 if data.shape[3] > 150 else 6
            fig, axes = plt.subplots(grid_shape, grid_shape, figsize=(s, s))
            label_classes = []
            for i, (im, axis) in enumerate(zip(data, axes.flatten())):
                axis.imshow(im.permute(1, 2, 0).cpu())
                if labels is not None and print_labels:
                    label_classes.append(classes[labels[i]])
                axis.axis('off')
            if print_labels:
                print(label_classes)


class UserMultiStep(UserSingleStep):
    """A user who computes multiple local update steps as in a FedAVG scenario."""

    def __init__(self, model, loss, dataloader, setup, cfg_user, num_queries=1):
        """Initialize but do not propagate the cfg_case.user dict further."""
        super().__init__(model, loss, dataloader, setup, cfg_user, num_queries)

        self.num_local_updates = cfg_user.num_local_updates
        self.num_data_per_local_update_step = cfg_user.num_data_per_local_update_step
        self.local_learning_rate = cfg_user.local_learning_rate
        self.provide_local_hyperparams = cfg_user.provide_local_hyperparams

    def compute_local_updates(self, server_payload):
        """Compute local updates to the given model based on server payload."""

        user_data, user_labels = self._generate_example_data()

        # Compute local updates
        shared_grads = []
        shared_buffers = []
        for query in range(self.num_user_queries):
            payload = server_payload['queries'][query]
            parameters = payload['parameters']
            buffers = payload['buffers']

            with torch.no_grad():
                for param, server_state in zip(self.model.parameters(), parameters):
                    param.copy_(server_state.to(**self.setup))
                for buffer, server_state in zip(self.model.buffers(), buffers):
                    buffer.copy_(server_state.to(**self.setup))

            optimizer = torch.optim.SGD(self.model.parameters(), lr=self.local_learning_rate)
            seen_data_idx = 0
            label_list = []
            for step in range(self.num_local_updates):
                data = user_data[seen_data_idx: seen_data_idx + self.num_data_per_local_update_step]
                labels = user_labels[seen_data_idx: seen_data_idx + self.num_data_per_local_update_step]
                seen_data_idx += self.num_data_per_local_update_step
                seen_data_idx = seen_data_idx % self.num_data_points
                label_list.append(labels)

                optimizer.zero_grad()
                # Compute the forward pass
                data_input = data + self.generator_inputi.sample(data.shape) if self.generator_input is not None else data
                outputs = self.model(data_input)
                loss = self.loss(outputs, labels)
                loss.backward()

                grads_ref = [p.grad for p in self.model.parameters()]
                if self.clip_value > 0:
                    self._clip_list_of_grad_(grads_ref)
                self._apply_differential_noise(grads_ref)
                optimizer.step()

            # Share differential to server version:
            # This is equivalent to sending the new stuff and letting the server do it, but in line
            # with the gradients sent in UserSingleStep
            shared_grads += [[(p_local - p_server.to(**self.setup)).clone().detach()
                              for (p_local, p_server) in zip(self.model.parameters(), parameters)]]
            shared_buffers += [[b.clone().detach() for b in self.model.buffers()]]

        shared_data = dict(gradients=shared_grads, buffers=shared_buffers,
                           num_data_points=self.num_data_points if self.provide_num_data_points else None,
                           labels=user_labels if self.provide_labels else None,
                           local_hyperparams=dict(lr=self.local_learning_rate, steps=self.num_local_updates,
                                                  data_per_step=self.num_data_per_local_update_step,
                                                  labels=label_list) if self.provide_local_hyperparams else None)
        true_user_data = dict(data=user_data, labels=user_labels)

        return shared_data, true_user_data


class MultiUserAggregate(UserMultiStep):
    """A silo of users who computes multiple local update steps as in a FedAVG scenario and aggregate their results.

       For an unaggregated single silo refer to SingleUser classes as above.
       A simple aggregate over multiple users in the FedSGD setting can better be modelled by the single user model above.
       """

    def __init__(self, model, loss, dataloader, setup, cfg_user, num_queries=1):
        """Initialize but do not propagate the cfg_case.user dict further."""
        super().__init__(model, loss, dataloader, setup, cfg_user, num_queries)

        self.num_users = cfg_user.num_users


    def _generate_example_data(self, user_idx=0):
        """Take care to partition data among users on the fly."""

        data_per_user = len(self.dataloader.dataset) // self.num_users
        user_data_subset = torch.utils.data.Subset(self.dataloader.dataset,
                                                   list(range(data_per_user * user_idx, (user_idx + 1) * data_per_user)))
        data = []
        labels = []
        pointer = self.data_idx
        for _ in range(self.num_data_points):
            datum, label = self.dataloader.dataset[pointer]
            data += [datum]
            labels += [torch.as_tensor(label)]
            if self.data_with_labels == 'unique':
                pointer += len(user_data_subset) // len(user_data_subset.dataset.classes)
            elif self.data_with_labels == 'same':
                pointer += 1
            elif self.data_with_labels == 'random':  # This will collide with the user thing
                pointer = torch.randint(0, len(user_data_subset), (1,))
            else:
                raise ValueError(f'Unknown {self.data_with_labels}')
            pointer = pointer % len(user_data_subset)  # Make sure not to leave the dataset range
        data = torch.stack(data).to(**self.setup)
        labels = torch.stack(labels).to(device=self.setup['device'])
        return data, labels

    def compute_local_updates(self, server_payload):
        """Compute local updates to the given model based on server payload."""
        # Compute local updates
        shared_grads = []
        shared_buffers = []
        for query in range(self.num_user_queries):
            payload = server_payload['queries'][query]
            server_parameters = payload['parameters']
            server_buffers = payload['buffers']

            aggregate_params = [torch.zeros_like(p) for p in self.model.parameters()]
            aggregate_buffers = [torch.zeros_like(b, dtype=torch.float) for b in self.model.buffers()]
            for user_idx in range(self.num_users):
                # Compute single user update
                user_data, user_labels = self._generate_example_data(user_idx)
                with torch.no_grad():
                    for param, server_state in zip(self.model.parameters(), server_parameters):
                        param.copy_(server_state.to(**self.setup))
                    for buffer, server_state in zip(self.model.buffers(), server_buffers):
                        buffer.copy_(server_state.to(**self.setup))

                optimizer = torch.optim.SGD(self.model.parameters(), lr=self.local_learning_rate)
                seen_data_idx = 0
                label_list = []
                for step in range(self.num_local_updates):
                    data = user_data[seen_data_idx: seen_data_idx + self.num_data_per_local_update_step]
                    labels = user_labels[seen_data_idx: seen_data_idx + self.num_data_per_local_update_step]
                    seen_data_idx += self.num_data_per_local_update_step
                    seen_data_idx = seen_data_idx % self.num_data_points
                    label_list.append(labels)

                    optimizer.zero_grad()
                    # Compute the forward pass
                    data_input = data + self.generator_input.sample(data.shape) if self.generator_input is not None else data
                    outputs = self.model(data_input)
                    loss = self.loss(outputs, labels)
                    loss.backward()

                    grads_ref = [p.grad for p in self.model.parameters()]
                    if self.clip_value > 0:
                        self._clip_list_of_grad_(grads_ref)
                    self._apply_differential_noise(grads_ref)
                    optimizer.step()
                # Add to running aggregates:

                # Share differential to server version:
                # This is equivalent to sending the new stuff and letting the server do it, but in line
                # with the gradients sent in UserSingleStep
                param_difference_to_server = torch._foreach_sub([p.cpu() for p in self.model.parameters()], server_parameters)
                torch._foreach_sub_(param_difference_to_server, aggregate_params)
                torch._foreach_add_(aggregate_params, param_difference_to_server, alpha=-1 / self.num_users)

                if len(aggregate_buffers) > 0:
                    buffer_to_server = [b.to(device=torch.device('cpu'), dtype=torch.float) for b in self.model.buffers()]
                    torch._foreach_sub_(buffer_to_server, aggregate_buffers)
                    torch._foreach_add_(aggregate_buffers, buffer_to_server, alpha=1 / self.num_users)

            shared_grads += [aggregate_params]
            shared_buffers += [aggregate_buffers]

        shared_data = dict(gradients=shared_grads, buffers=shared_buffers,
                           num_data_points=self.num_data_points if self.provide_num_data_points else None,
                           labels=user_labels if self.provide_labels else None, num_users=self.num_users,
                           local_hyperparams=dict(lr=self.local_learning_rate, steps=self.num_local_updates,
                                                  data_per_step=self.num_data_per_local_update_step,
                                                  labels=label_list) if self.provide_local_hyperparams else None)

        def generate_user_data():
            for user_idx in range(self.num_users):
                yield self._generate_example_data(user_idx)[0]

        true_user_data = dict(data=generate_user_data(),
                              labels=None)

        return shared_data, true_user_data
