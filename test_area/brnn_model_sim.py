
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import time
import numpy as np
import tensorflow as tf
import util
import reader
import subprocess
from tensorflow.contrib.rnn import BasicLSTMCell, LSTMStateTuple, MultiRNNCell
from tensorflow.contrib.distributions import Normal
import import_folders 
import pickle_lib as pkl

"""
    Global variables
"""
model_type = "small"
data_path = "../data/"
save_path = "./saved_model"
global_prior_pi = 0.25
global_log_sigma1 = -1.0
global_log_sigma2 = -7.0
global_random_seed = 12


def data_type():
    return tf.float32


def get_config():
    """Get model config."""
    if model_type == "small":
        config = SmallConfig()
    elif model_type == "medium":
        config = MediumConfig()
    elif model_type == "large":
        config = LargeConfig()
    elif model_type == "test":
        config = TestConfig()
    else:
        raise ValueError("Invalid model: %s", model_type)

    config.prior_pi = global_prior_pi
    config.log_sigma1 = global_log_sigma1
    config.log_sigma2 = global_log_sigma2

    return config


def sample_posterior(shape, name, prior, is_training):

    """
        Get a sample from the multivariate posterior
    """
    
    
    rho_max_init = math.log(math.exp(prior.sigma_mix / 2.0) - 1.0)
    rho_min_init = math.log(math.exp(prior.sigma_mix / 4.0) - 1.0)
    
    init = tf.random_uniform_initializer(rho_min_init, rho_max_init)
    
    with tf.variable_scope("BBB", reuse = not is_training):
        mu = tf.get_variable(name + "_mean", shape = shape, dtype=data_type())
    
    with tf.variable_scope("BBB", reuse = not is_training):
        rho = tf.get_variable(name + "_rho", shape = shape, dtype=data_type(), initializer=init)
        
    if is_training:
        epsilon = Normal(0.0, 1.0).sample(shape)
        sigma = tf.nn.softplus(rho) + 1e-5
        output = mu + sigma * epsilon
    else:
        output = mu

    if not is_training:
        return output
    
    tf.summary.histogram(name + '_rho_hist', rho)
    tf.summary.histogram(name + '_mu_hist', mu)
    tf.summary.histogram(name + '_sigma_hist', sigma)

    sample = output
    kl = get_kl_divergence(shape, tf.reshape(mu, [-1]), tf.reshape(sigma, [-1]), prior, sample)
    tf.add_to_collection('KL_layers', kl)

    return output


def get_kl_divergence(shape, mu, sigma, prior, sample):
    

    """
    Compute KL divergence between posterior and prior.
    log(q(theta)) - log(p(theta)) where
    p(theta) = pi*N(0,sigma1) + (1-pi)*N(0,sigma2)
    
    shape = shape of the sample we want to compute the KL of
    mu = the mu variable used when sampling
    sigma= the sigma variable used when sampling
    prior = the prior object with parameters
    sample = the sample from the posterior
    """
    
    #Flatten to a vector
    sample = tf.reshape(sample, [-1])
    
    #Get the log probability distribution of your sampled variable
    #So essentially get: q( theta | mu, sigma )
    posterior = Normal(mu, sigma)
    
    
    prior_1 = Normal(0.0, prior.sigma1)
    prior_2 = Normal(0.0, prior.sigma2)
    
    #get: sum( log[ q( theta | mu, sigma ) ] )
    q_theta = tf.reduce_sum(posterior.log_prob(sample))
    
    #get: sum( log[ p( theta ) ] ) for mixture prior
    mix1 = tf.reduce_sum(prior_1.log_prob(sample)) + tf.log(prior.pi_mix)
    mix2 = tf.reduce_sum(prior_2.log_prob(sample)) + tf.log(1.0 - prior.pi_mix)
    
    #Compute KL distance
    KL = q_theta - tf.reduce_logsumexp([mix1,mix2])
    
    return KL


class Prior(object):

    """
        For creating our fixed prior containing the desired 
        properties we want to use in the model.
        
        Setting pi = 1 will lead to a non mixture gaussian
        with mean zero and log var = log_sigma1
    """

    def __init__(self, pi, log_sigma1, log_sigma2):
        self.pi_mix = pi
        self.log_sigma1 = log_sigma1
        self.log_sigma2 = log_sigma2
        self.sigma1 = tf.exp(log_sigma1)
        self.sigma2 = tf.exp(log_sigma2)
        sigma_one, sigma_two = math.exp(log_sigma1), math.exp(log_sigma2)
        self.sigma_mix = np.sqrt(pi * np.square(sigma_one) + (1.0 - pi) * np.square(sigma_two))


class BayesianLSTMCell(BasicLSTMCell):
    def __init__(self, num_units, prior, is_training, name = None, **kwargs):
        
        super(BayesianLSTMCell, self).__init__(num_units, **kwargs)

        self.w = None
        self.b = None
        self.prior = prior
        self.n = name
        self.is_training = is_training
        self.num_units = num_units
        

    def call(self, inputs, state):
        if self.w is None:

            size = inputs.get_shape()[-1].value
            
            self.w = sample_posterior((size + self.num_units, 4 * self.num_units),
                                          name=self.n + "_weights",
                                          prior=self.prior,
                                          is_training=self.is_training)

            self.b = sample_posterior((4 * self.num_units, 1),
                                           name=self.n + "_biases",
                                           prior=self.prior,
                                           is_training=self.is_training)

        cell, hidden = state
        print("Cell shape: ", cell.get_shape())   
        print("Hidden shape: ", hidden.get_shape())       


        concat_inputs_hidden = tf.concat([inputs, hidden],1)
        print("Concat_inputs_hidden: ", concat_inputs_hidden.get_shape())
        
        concat_inputs_hidden = tf.nn.bias_add(tf.matmul(concat_inputs_hidden, self.w), tf.squeeze(self.b))
        print("Concat_inputs_hidden (adding bias): ", concat_inputs_hidden.get_shape())
        
        i, j, f, o = tf.split(value=concat_inputs_hidden, num_or_size_splits=4, axis=1)

        new_cell = (cell * tf.sigmoid(f + self._forget_bias) + tf.sigmoid(i) * self._activation(j))
        print("New Cell: ", new_cell.get_shape())
        
        new_hidden = self._activation(new_cell) * tf.sigmoid(o)
        print("New Hidden: ", new_hidden.get_shape())

        new_state = LSTMStateTuple(new_cell, new_hidden)
        
        return new_hidden, new_state


class PTBInput(object):
    """The input data."""

    def __init__(self, config, data, name=None):
        self.batch_size = batch_size = config.batch_size
        self.num_steps = num_steps = config.num_steps
        self.epoch_size = ((len(data) // batch_size) - 1) // num_steps
        self.input_data, self.targets = reader.ptb_producer(
        data, batch_size, num_steps, name=name)


class SIMInput(object):
    """The simulated input data."""

    def __init__(self, config, x, y, name=None):
        self.batch_size = batch_size = config.batch_size
        self.num_steps = num_steps = config.num_steps
        self.epoch_size = ((len(x) // batch_size) - 1) // num_steps
        self.input_data, self.targets = sim_producer(
        x, y , batch_size, num_steps, name=name)
        
        
def sim_producer(x, y, batch_size, num_steps, name=None):
  """Iterate on SIMULATED data.

  This chunks up raw_data into batches of examples and returns Tensors that
  are drawn from these batches.

  Args:
    raw_data: one of the raw data outputs from ptb_raw_data.
    batch_size: int, the batch size.
    num_steps: int, the number of unrolls.
    name: the name of this operation (optional).

  Returns:
    A pair of Tensors, each shaped [batch_size, num_steps]. The second element
    of the tuple is the same data time-shifted to the right by one.

  Raises:
    tf.errors.InvalidArgumentError: if batch_size or num_steps are too high.
  """
  with tf.name_scope(name, "SIMProducer", [x, batch_size, num_steps]):
    x_data = tf.convert_to_tensor(x, name="x_data", dtype=tf.float32)
    y_data = tf.convert_to_tensor(y, name="y_data", dtype=tf.int32)
    print("Finished tf.convert_to_tensor")
    
    data_len = tf.size(x_data)
    batch_len = data_len // batch_size
    x_data = tf.reshape(x_data[0 : batch_size * batch_len],
                      [batch_size, batch_len])
    y_data = tf.reshape(y_data[0 : batch_size * batch_len],
                      [batch_size, batch_len])

    epoch_size = (batch_len - 1) // num_steps
    assertion = tf.assert_positive(
        epoch_size,
        message="epoch_size == 0, decrease batch_size or num_steps")
    with tf.control_dependencies([assertion]):
      epoch_size = tf.identity(epoch_size, name="epoch_size")

    i = tf.train.range_input_producer(epoch_size, shuffle=False).dequeue()
    x = tf.strided_slice(x_data, [0, i * num_steps],
                         [batch_size, (i + 1) * num_steps])
    x.set_shape([batch_size, num_steps])
    y = tf.strided_slice(y_data, [0, i * num_steps + 1],
                         [batch_size, (i + 1) * num_steps + 1])
    y.set_shape([batch_size, num_steps])
    return x, y



def get_All_datapoints(folder_data = "./artificial_data/", Ndivisions = 10):
    X_list = pkl.load_pickle(folder_data +"X_values.pkl",Ndivisions)
    Y_list = pkl.load_pickle(folder_data +"Y_values.pkl",Ndivisions)
    X_all = np.concatenate((X_list), axis = 0)
    Y_all = np.concatenate((Y_list), axis = 0)
    
    return X_all, Y_all


def split_data(y, tr, val, te):
    """
    Description: Split data set into train, validation and test sets.
                 Equivalent to PTB_raw_data()
    INPUT: 1. Entire Dataset Inputs (x)
           2. Entire Dataset Outputs (y, aka targets)
           3-5. Fractions of splits. Should sum up to 1.
    OUTPUT: 
    """
    assert (tr + val + te) == 1
    #assert x.shape[0] == y.shape[0] #Making sure num of rows match
    # Split Data
    
    train = y[:int(len(y)*tr)]
    valid = y[int(len(y)*tr):int(len(y)*val)]
    test = y[int(len(y)*val):]
    
    """
    # Creating Placeholders
    train_x_ph = tf.placeholder(train_x.dtype, train_x.shape)
    train_y_ph = tf.placeholder(train_y.dtype, train_y.shape)
    valid_x_ph = tf.placeholder(valid_x.dtype, train_x.shape)
    valid_y_ph = tf.placeholder(valid_y.dtype, train_y.shape)
    test_x_ph = tf.placeholder(test_x.dtype, test_x.shape)
    test_y_ph = tf.placeholder(test_y.dtype, test_y.shape)
    
    # Organizing datasets
    train_data = tf.data.Dataset.from_tensor_slices((train_x_ph, train_y_ph))
    valid_data = tf.data.Dataset.from_tensor_slices((valid_x_ph, valid_y_ph))
    test_data = tf.data.Dataset.from_tensor_slices((test_x_ph, test_y_ph))
    # [ ] Make iterable
    # [ ] Do we include the feed_dict = { : , : } here? 
    """
    return train, valid, test

class PTBModel(object):
    def __init__(self, is_training, config, input_):
        self._is_training = is_training
        self._input = input_
        self.batch_size = input_.batch_size
        self.num_steps = input_.num_steps
        size = config.hidden_size
        vocab_size = config.vocab_size
        
        # Construct prior
        prior = Prior(config.prior_pi, config.log_sigma1, config.log_sigma2)
       
        """
        # Fetch embeddings
        with tf.device("/cpu:0"):
            embedding = sample_posterior([vocab_size, size], "embedding", prior, is_training)
            inputs = tf.nn.embedding_lookup(embedding, input_.input_data)
        """
        # Making Simulated data match dims of PTB data
        # Dims: [batch size, numsteps, hidden size]
        inputs = input_.input_data
        inputs = tf.expand_dims(tf.ones([self.batch_size, self.num_steps]), 0)*inputs
        inputs = tf.tile(inputs, [size,1,1])
        inputs = tf.transpose(inputs, perm=[1, 2, 0])
        print("Input data Shape: ", inputs.get_shape())
        
        # Build the BBB LSTM cells
        cells = []
        for i in range(config.num_layers):
            cells.append(BayesianLSTMCell(size, prior, is_training,
                                      forget_bias=0.0,
                                      name="bbb_lstm_{}".format(i)))
            
        print(1)    
        cell = MultiRNNCell(cells, state_is_tuple=True)
        print(2)
        self._initial_state = cell.zero_state(config.batch_size, data_type())
        state = self._initial_state
        print(3)

        # Forward pass for the truncated mini-batch
        outputs = []
        with tf.variable_scope("RNN"):
            for time_step in range(self.num_steps):
                if time_step > 0: tf.get_variable_scope().reuse_variables()
                (cell_output, state) = cell(inputs[:, time_step, :], state)
                outputs.append(cell_output)
        output = tf.reshape(tf.concat(outputs, 1), [-1, config.hidden_size])
        print("Output: ", output.get_shape())

        # Softmax weights
        softmax_w = sample_posterior((size, vocab_size), "softmax_w", prior, is_training)
        softmax_b = sample_posterior((vocab_size, 1), "softmax_b", prior, is_training)
        print("Softmax_w: ", softmax_w.get_shape())
        print("Softmax_b: ", softmax_b.get_shape())

        logits = tf.nn.xw_plus_b(output, softmax_w, tf.squeeze(softmax_b))
        logits = tf.reshape(logits, [self.batch_size, self.num_steps, vocab_size])
        print("Logits: ", logits.get_shape())

        loss = tf.contrib.seq2seq.sequence_loss(
            logits,
            input_.targets,
            tf.ones([self.batch_size, self.num_steps], dtype=data_type()),
            average_across_timesteps=False,
            average_across_batch=False)

        # Update the cost
        # Remember to divide by batch size
        self._cost = tf.reduce_sum(loss) / self.batch_size
        self._kl_loss = 0.
        self._final_state = state
        
        if not is_training:
            return

        #Compute KL divergence
        #B = number of batches aka the epoch size
        #C = number of truncated sequences in a batch aka batch_size variable
        B = self._input.epoch_size
        C = self.batch_size
        
        kl_loss = tf.add_n(tf.get_collection("KL_layers"), "kl_divergence")
        
        kl_factor = 1.0/(B*C)
        self._kl_loss = kl_factor * kl_loss
        
        self._total_loss = self._cost + self._kl_loss

        self._lr = tf.Variable(0.0, trainable=False)
        tvars = tf.trainable_variables()
        grads, _ = tf.clip_by_global_norm(tf.gradients(self._total_loss, tvars),
                                          config.max_grad_norm)
        optimizer = tf.train.GradientDescentOptimizer(self._lr)
        self._train_op = optimizer.apply_gradients(
            zip(grads, tvars),
            global_step=tf.contrib.framework.get_or_create_global_step())

        self._new_lr = tf.placeholder(data_type(), shape=[], name="new_learning_rate")
        self._lr_update = tf.assign(self._lr, self._new_lr)

    def assign_lr(self, session, lr_value):
        session.run(self._lr_update, feed_dict={self._new_lr: lr_value})

    def export_ops(self, name):
        """Exports ops to collections."""
        self._name = name
        ops = {util.with_prefix(self._name, "cost"): self._cost,
               util.with_prefix(self._name, "kl_div"): self._kl_loss}
        if self._is_training:
            ops.update(lr=self._lr, new_lr=self._new_lr, lr_update=self._lr_update)
        for name, op in ops.items():
            tf.add_to_collection(name, op)
        self._initial_state_name = util.with_prefix(self._name, "initial")
        self._final_state_name = util.with_prefix(self._name, "final")
        util.export_state_tuples(self._initial_state, self._initial_state_name)
        util.export_state_tuples(self._final_state, self._final_state_name)

    def import_ops(self):
        """Imports ops from collections."""
        if self._is_training:
            self._train_op = tf.get_collection_ref("train_op")[0]
            self._lr = tf.get_collection_ref("lr")[0]
            self._new_lr = tf.get_collection_ref("new_lr")[0]
            self._lr_update = tf.get_collection_ref("lr_update")[0]
        self._cost = tf.get_collection_ref(util.with_prefix(self._name, "cost"))[0]
        self._kl_loss = tf.get_collection_ref(util.with_prefix(self._name, "kl_div"))[0]
        num_replicas = 1
        self._initial_state = util.import_state_tuples(
            self._initial_state, self._initial_state_name, num_replicas)
        self._final_state = util.import_state_tuples(
            self._final_state, self._final_state_name, num_replicas)

    @property
    def input(self):
        return self._input

    @property
    def initial_state(self):
        return self._initial_state

    @property
    def cost(self):
        return self._cost

    @property
    def final_state(self):
        return self._final_state

    @property
    def lr(self):
        return self._lr

    @property
    def train_op(self):
        return self._train_op

    @property
    def initial_state_name(self):
        return self._initial_state_name

    @property
    def final_state_name(self):
        return self._final_state_name

    @property
    def kl_loss(self):
        return self._kl_loss if self._is_training else tf.constant(0.)


class SmallConfig(object):
    """Small config."""
    init_scale = 0.1
    learning_rate = 1.0
    max_grad_norm = 5
    num_layers = 2
    num_steps = 20
    hidden_size = 200
    max_epoch = 4
    max_max_epoch = 13
    keep_prob = 1.0
    lr_decay = 0.5
    batch_size = 20
    vocab_size = 10000


class MediumConfig(object):
    """
    Medium config.
    Slightly modified according to email.
    """
    init_scale = 0.05
    learning_rate = 1.0
    max_grad_norm = 5
    num_layers = 2
    num_steps = 35
    hidden_size = 650
    max_epoch = 20
    max_max_epoch = 70
    keep_prob = 1.0
    lr_decay = 0.9
    batch_size = 20
    vocab_size = 10000


class LargeConfig(object):
    """Large config."""
    init_scale = 0.04
    learning_rate = 1.0
    max_grad_norm = 10
    num_layers = 2
    num_steps = 35
    hidden_size = 1500
    max_epoch = 14
    max_max_epoch = 55
    keep_prob = 0.35
    lr_decay = 1 / 1.15
    batch_size = 20
    vocab_size = 10000


class TestConfig(object):
    """Tiny config, for testing."""
    init_scale = 0.1
    learning_rate = 1.0
    max_grad_norm = 1
    num_layers = 1
    num_steps = 2
    hidden_size = 2
    max_epoch = 1
    max_max_epoch = 1
    keep_prob = 1.0
    lr_decay = 0.5
    batch_size = 20
    vocab_size = 10000


def run_epoch(session, model, eval_op=None, verbose=False):
    """Runs the model on the given data."""
    start_time = time.time()
    costs = 0.0
    iters = 0
    state = session.run(model.initial_state)
    
    fetches = {
        "cost": model.cost,
        "final_state": model.final_state,
    }
    if eval_op is not None:
        fetches["eval_op"] = eval_op
        fetches["kl_divergence"] = model.kl_loss

    for step in range(model.input.epoch_size):
        feed_dict = {}
        for i, (c, h) in enumerate(model.initial_state):
            feed_dict[c] = state[i].c
            feed_dict[h] = state[i].h

        vals = session.run(fetches, feed_dict)
        cost = vals["cost"]
        state = vals["final_state"]

        costs += cost
        iters += model.input.num_steps

        if verbose and (step % (model.input.epoch_size // 10) == 10 or step == 0):
            print("%.3f perplexity: %.3f speed: %.0f wps" %
                  (step * 1.0 / model.input.epoch_size, np.exp(costs / iters),
                   iters * model.input.batch_size / (time.time() - start_time)))

            if model._is_training:
                print("KL is {}".format(vals["kl_divergence"]))

    return np.exp(costs / iters)


#def change_random_seed(seed):
#    global prng
#    prng = np.random.RandomState(seed)
#    tf.set_random_seed(seed)


def main(model_select="small",
         dat_path = "../data",
         sav_path = "./saved_model/",
         mixing_pi = 0.25,
         prior_log_sigma1 = -1.0,
         prior_log_sigma2 = -7.0):
    
    global model_type
    global data_path
    global save_path
    global global_prior_pi
    global global_log_sigma1
    global global_log_sigma2
#    global global_random_seed
    
    model_type = model_select
    data_path = dat_path
    save_path = sav_path
    global_prior_pi = mixing_pi
    global_log_sigma1 = prior_log_sigma1
    global_log_sigma2 = prior_log_sigma2
#    global_random_seed = set_random_seed

#    change_random_seed(global_random_seed)
    #raw_data = reader.ptb_raw_data(data_path)
    #train_data, valid_data, test_data, _ = raw_data
    x, y = get_All_datapoints()
    print("get_All_datpoints():\t", "X len: ", len(x), "Y len: ", len(y))
    train_data_x, valid_data_x, test_data_x = split_data(x,0.8,0.1,0.1)
    train_data_y, valid_data_y, test_data_y = split_data(y,0.8,0.1,0.1)
    print("split_data():\t  TrainX len: ", len(train_data_x) )


    config = get_config()
    eval_config = get_config()
    eval_config.batch_size = 1
    eval_config.num_steps = 1

    subprocess.Popen(["tensorboard","--logdir=tensorboard"])

    with tf.Graph().as_default():
        initializer = tf.random_uniform_initializer(-config.init_scale,
                                                    config.init_scale)

        with tf.name_scope("Train"):
            train_input = SIMInput(config=config, x=train_data_x, y=train_data_y, name="TrainInput")
            with tf.variable_scope("Model", reuse=None, initializer=initializer):
                m = PTBModel(is_training=True, config=config, input_=train_input)
            tf.summary.scalar("Training_Loss", m.cost)
            tf.summary.scalar("Learning_Rate", m.lr)

        with tf.name_scope("Valid"):
            valid_input = SIMInput(config=config, x=valid_data_x, y=valid_data_y, name="ValidInput")
            with tf.variable_scope("Model", reuse=True, initializer=initializer):
                mvalid = PTBModel(is_training=False, config=config, input_=valid_input)
            tf.summary.scalar("Validation_Loss", mvalid.cost)

        with tf.name_scope("Test"):
            test_input = SIMInput(
                config=eval_config, x=test_data_x, y=test_data_y, name="TestInput")
            with tf.variable_scope("Model", reuse=True, initializer=initializer):
                mtest = PTBModel(is_training=False, config=eval_config,
                                 input_=test_input)

        models = {"Train": m, "Valid": mvalid, "Test": mtest}
        for name, model in models.items():
            model.export_ops(name)
        metagraph = tf.train.export_meta_graph()
        soft_placement = False

    with tf.Graph().as_default():
        tf.train.import_meta_graph(metagraph)
        for model in models.values():
            model.import_ops()
        sv = tf.train.Supervisor(logdir=save_path)
        config_proto = tf.ConfigProto(allow_soft_placement=soft_placement)
        with sv.managed_session(config=config_proto) as session:
            for i in range(config.max_max_epoch):
                lr_decay = config.lr_decay ** max(i + 1 - config.max_epoch, 0.0)
                m.assign_lr(session, config.learning_rate * lr_decay)

                print("Epoch: %d Learning rate: %.3f" % (i + 1, session.run(m.lr)))
                train_perplexity = run_epoch(session, m, eval_op=m.train_op,
                                             verbose=True)
                print("Epoch: %d Train Perplexity: %.3f" % (i + 1, train_perplexity))
                valid_perplexity = run_epoch(session, mvalid)
                print("Epoch: %d Valid Perplexity: %.3f" % (i + 1, valid_perplexity))

            test_perplexity = run_epoch(session, mtest)
            print("Test Perplexity: %.3f" % test_perplexity)

            if save_path:
                print("Saving model to %s." % save_path)
                sv.saver.save(session, save_path, global_step=sv.global_step)


if __name__ == '__main__':
    main()