import random
import tensorflow as tf
import numpy as np
import miditoolkit
import modules
import pickle
import utils
import time
import transpose

# run as tf1
import tensorflow.compat.v1 as tf

tf.disable_v2_behavior()


class PopMusicTransformer(object):
    ########################################
    # initialize
    ########################################
    def __init__(self,
                 checkpoint_path,
                 dictionary_path,
                 is_training=False,
                 x_len=512,
                 mem_len=512,
                 n_layer=12,
                 d_embed=512,
                 d_model=512,
                 dropout=0.1,
                 n_head=8,
                 d_head=None,
                 d_ff=2048,
                 learning_rate=0.0002,
                 use_chords=True,
                 group_size=5,
                 transpose_input_midi_to_key=None,
                 exchangeable_words=None,
                 transpose_to_all_keys=False
                 ):
        if exchangeable_words is None:
            exchangeable_words = []

        # Reset tensorflow default graph
        tf.reset_default_graph()

        # load dictionary
        self.dictionary_path = dictionary_path
        self.event2word, self.word2event = pickle.load(open(self.dictionary_path, 'rb'))

        # model settings
        self.x_len = x_len
        self.mem_len = mem_len
        self.n_layer = n_layer
        self.d_embed = d_embed
        self.d_model = d_model
        self.dropout = dropout
        self.n_head = n_head
        if d_head is None:
            self.d_head = self.d_model // self.n_head
        else:
            self.d_head = d_head
        self.d_ff = d_ff
        self.n_token = len(self.event2word)
        self.learning_rate = learning_rate
        # load model
        self.is_training = is_training
        if self.is_training:
            self.batch_size = 4
        else:
            self.batch_size = 1
        self.checkpoint_path = checkpoint_path
        self.use_chords = use_chords
        self.group_size = group_size
        self.transpose_input_midi_to_key = transpose_input_midi_to_key
        self.exchangeable_words = [[self.event2word[x] for x in y] for y in exchangeable_words]
        self.transpose_to_all_keys = transpose_to_all_keys
        self.create_model()

    ########################################
    # create model
    ########################################
    def create_model(self):
        # placeholders
        self.x = tf.compat.v1.placeholder(tf.int32, shape=[self.batch_size, None])
        self.y = tf.compat.v1.placeholder(tf.int32, shape=[self.batch_size, None])
        self.mems_i = [tf.compat.v1.placeholder(tf.float32, [self.mem_len, self.batch_size, self.d_model]) for _ in
                       range(self.n_layer)]
        # model
        self.global_step = tf.compat.v1.train.get_or_create_global_step()
        initializer = tf.compat.v1.initializers.random_normal(stddev=0.02, seed=None)
        proj_initializer = tf.compat.v1.initializers.random_normal(stddev=0.01, seed=None)
        with tf.compat.v1.variable_scope(tf.compat.v1.get_variable_scope()):
            xx = tf.transpose(self.x, [1, 0])
            yy = tf.transpose(self.y, [1, 0])
            loss, self.logits, self.new_mem = modules.transformer(
                dec_inp=xx,
                target=yy,
                mems=self.mems_i,
                n_token=self.n_token,
                n_layer=self.n_layer,
                d_model=self.d_model,
                d_embed=self.d_embed,
                n_head=self.n_head,
                d_head=self.d_head,
                d_inner=self.d_ff,
                dropout=self.dropout,
                dropatt=self.dropout,
                initializer=initializer,
                proj_initializer=proj_initializer,
                is_training=self.is_training,
                mem_len=self.mem_len,
                cutoffs=[],
                div_val=-1,
                tie_projs=[],
                same_length=False,
                clamp_len=-1,
                input_perms=None,
                target_perms=None,
                head_target=None,
                untie_r=False,
                proj_same_dim=True)
        self.avg_loss = tf.reduce_mean(loss)
        # vars
        all_vars = tf.compat.v1.trainable_variables()
        grads = tf.gradients(self.avg_loss, all_vars)
        grads_and_vars = list(zip(grads, all_vars))
        all_trainable_vars = tf.reduce_sum([tf.reduce_prod(v.shape) for v in tf.compat.v1.trainable_variables()])
        # optimizer
        decay_lr = tf.compat.v1.train.cosine_decay(
            self.learning_rate,
            global_step=self.global_step,
            decay_steps=400000,
            alpha=0.004)
        optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate=decay_lr)
        self.train_op = optimizer.apply_gradients(grads_and_vars, self.global_step)
        # saver
        self.saver = tf.compat.v1.train.Saver()
        config = tf.compat.v1.ConfigProto(allow_soft_placement=True)
        config.gpu_options.allow_growth = True
        self.sess = tf.compat.v1.Session(config=config)
        if self.checkpoint_path is not None:
            self.saver.restore(self.sess, self.checkpoint_path)
        else:
            init_op = tf.initialize_all_variables()
            self.sess.run(init_op)

    ########################################
    # temperature sampling
    ########################################
    def temperature_sampling(self, logits, temperature, topk):
        probs = np.exp(logits / temperature) / np.sum(np.exp(logits / temperature))
        if topk == 1:
            prediction = np.argmax(probs)
        else:
            sorted_index = np.argsort(probs)[::-1]
            candi_index = sorted_index[:topk]
            candi_probs = [probs[i] for i in candi_index]
            # normalize probs
            candi_probs /= sum(candi_probs)
            # choose by predicted probs
            prediction = np.random.choice(candi_index, size=1, p=candi_probs)[0]
        return prediction

    ########################################
    # extract events for prompt continuation
    ########################################
    def extract_events(self, input_path, transposition_steps=0):
        if self.transpose_input_midi_to_key:
            transposition_steps = transpose.get_number_of_steps_for_transposition_to(input_path,
                                                                                     self.transpose_input_midi_to_key)
        if transposition_steps != 0:
            print("Transposing {} steps to {}.".format(transposition_steps, self.transpose_input_midi_to_key))

        note_items, tempo_items = utils.read_items(input_path, transposition_steps=transposition_steps)
        note_items = utils.quantize_items(note_items)
        max_time = note_items[-1].end
        if self.use_chords:
            chord_items = utils.extract_chords(note_items)
            items = chord_items + tempo_items + note_items
        else:
            items = tempo_items + note_items
        groups = utils.group_items(items, max_time)
        events = utils.item2event(groups)
        return events

    ########################################
    # generate
    ########################################
    def generate_batch(self, number_of_results, n_target_bar, temperature, topk, output_path, prompt=None):
        for i in range(number_of_results):
            self.generate(n_target_bar, temperature, topk, output_path + "/result_{}.midi".format(i), prompt)

    def generate(self, n_target_bar, temperature, topk, output_path, prompt=None):
        # if prompt, load it. Or, random start
        number_of_bars_in_prompt = 0
        first_note = True
        duration_classes = [v for k, v in self.event2word.items() if 'Note Duration' in k]

        if prompt:
            events = self.extract_events(prompt)
            words = [[self.event2word['{}_{}'.format(e.name, e.value)] for e in events]]
            words[0].append(self.event2word['Bar_None'])
            number_of_bars_in_prompt = words[0].count(self.event2word['Bar_None']) - 1
        else:
            words = []
            for _ in range(self.batch_size):
                ws = [self.event2word['Bar_None']]
                if 'chord' in self.checkpoint_path:
                    tempo_classes = [v for k, v in self.event2word.items() if 'Tempo Class' in k]
                    tempo_values = [v for k, v in self.event2word.items() if 'Tempo Value' in k]
                    chords = [v for k, v in self.event2word.items() if 'Chord' in k]
                    ws.append(self.event2word['Position_1/16'])
                    ws.append(np.random.choice(chords))
                    ws.append(self.event2word['Position_1/16'])
                    ws.append(np.random.choice(tempo_classes))
                    ws.append(np.random.choice(tempo_values))
                else:
                    tempo_classes = [v for k, v in self.event2word.items() if 'Tempo Class' in k]
                    tempo_values = [v for k, v in self.event2word.items() if 'Tempo Value' in k]
                    ws.append(self.event2word['Position_1/16'])
                    ws.append(np.random.choice(tempo_classes))
                    ws.append(np.random.choice(tempo_values))
                words.append(ws)
        # initialize mem
        batch_m = [np.zeros((self.mem_len, self.batch_size, self.d_model), dtype=np.float32) for _ in
                   range(self.n_layer)]
        # generate
        original_length = len(words[0])
        initial_flag = 1
        current_generated_bar = 0
        while current_generated_bar < n_target_bar:
            # input
            if initial_flag:
                temp_x = np.zeros((self.batch_size, original_length))
                for b in range(self.batch_size):
                    for z, t in enumerate(words[b]):
                        temp_x[b][z] = t
                initial_flag = 0
            else:
                temp_x = np.zeros((self.batch_size, 1))
                for b in range(self.batch_size):
                    temp_x[b][0] = words[b][-1]
            # prepare feed dict
            feed_dict = {self.x: temp_x}
            for m, m_np in zip(self.mems_i, batch_m):
                feed_dict[m] = m_np
            # model (prediction)
            _logits, _new_mem = self.sess.run([self.logits, self.new_mem], feed_dict=feed_dict)
            # sampling
            _logit = _logits[-1, 0]
            word = self.temperature_sampling(
                logits=_logit,
                temperature=temperature,
                topk=topk)

            # First note gets a completely random duration
            if first_note and 'Note Duration' in self.word2event[word]:
                word = np.random.choice(duration_classes)
                first_note = False

            words[0].append(word)
            # if bar event (only work for batch_size=1)
            if word == self.event2word['Bar_None']:
                current_generated_bar += 1
            # re-new mem
            batch_m = _new_mem
        # write
        if prompt:
            utils.write_midi(
                words=words[0][original_length:],
                word2event=self.word2event,
                output_path=output_path,
                prompt_path=prompt,
                bars_in_prompt=number_of_bars_in_prompt)
        else:
            utils.write_midi(
                words=words[0],
                word2event=self.word2event,
                output_path=output_path,
                prompt_path=None)

    ########################################
    # prepare training data
    ########################################
    def prepare_data(self, midi_paths):
        # extract events
        segments = []

        transposition_steps = [0]
        if self.transpose_to_all_keys:
            transposition_steps = [-2, -1, 0, 1, 2, 3, 4, 5]

        for path in midi_paths:
            for transposition_step in transposition_steps:
                try:
                    all_events = []

                    print(f"Extracting events for {path}")
                    events = self.extract_events(path, transposition_step)
                    all_events.append(events)

                    # event to word
                    all_words = []
                    for events in all_events:
                        words = []
                        for event in events:
                            e = '{}_{}'.format(event.name, event.value)
                            if e in self.event2word:
                                words.append(self.event2word[e])
                            else:
                                # OOV
                                if event.name == 'Note Velocity':
                                    # replace with max velocity based on our training data
                                    words.append(self.event2word['Note Velocity_21'])
                                else:
                                    # something is wrong
                                    # you should handle it for your own purpose
                                    print('something is wrong! {}'.format(e))
                        all_words.append(words)

                    # to training data
                    new_segments = []
                    for words in all_words:
                        pairs = []
                        for i in range(0, len(words) - self.x_len - 1, self.x_len):
                            x = words[i:i + self.x_len]
                            y = words[i + 1:i + self.x_len + 1]
                            pairs.append([x, y])
                        pairs = np.array(pairs)
                        # abandon the last
                        for i in np.arange(0, len(pairs) - self.group_size, self.group_size * 2):
                            data = pairs[i:i + self.group_size]
                            if len(data) == self.group_size:
                                new_segments.append(data)

                    # Create reverse segments
                    for words in all_words:
                        pairs = []
                        for i in range(len(words) - 1, self.x_len, -self.x_len):
                            x = words[i - self.x_len - 1:i - 1]
                            y = words[i - self.x_len:i]
                            pairs.append([x, y])
                        pairs = np.array(pairs[::-1])
                        # abandon the last
                        for i in np.arange(0, len(pairs) - self.group_size, self.group_size * 2):
                            data = pairs[i:i + self.group_size]
                            if len(data) == self.group_size:
                                new_segments.append(data)

                    print(f"Prepared {len(new_segments)} segments.")
                    segments.extend(new_segments)
                except Exception as e:
                    print(f"error processing {path}, error: {e}")
        segments = np.array(segments)
        return segments

    ########################################
    # finetune
    ########################################
    def finetune(self, training_data, output_checkpoint_folder, epochs=200, stop_loss=None, save_checkpoint_batch=100):
        # shuffle
        index = np.arange(len(training_data))
        np.random.shuffle(index)
        training_data = training_data[index]
        num_batches = len(training_data) // self.batch_size
        print('num_batches:', num_batches)
        print('training_data.shape:', training_data.shape)
        st = time.time()
        for e in range(epochs):
            total_loss = []
            for i in range(num_batches):
                segments = training_data[self.batch_size * i:self.batch_size * (i + 1)]

                batch_m = [np.zeros((self.mem_len, self.batch_size, self.d_model), dtype=np.float32) for _ in
                           range(self.n_layer)]

                for j in range(self.group_size):
                    try:
                        batch_x = segments[:, j, 0, :]
                        batch_y = segments[:, j, 1, :]

                        # Exchange words
                        if self.exchangeable_words is not None and len(self.exchangeable_words) > 0:
                            self.exchange_words(batch_x, batch_y)

                        # prepare feed dict
                        feed_dict = {self.x: batch_x, self.y: batch_y}
                        for m, m_np in zip(self.mems_i, batch_m):
                            feed_dict[m] = m_np
                        # run
                        _, gs_, loss_, new_mem_ = self.sess.run(
                            [self.train_op, self.global_step, self.avg_loss, self.new_mem], feed_dict=feed_dict)
                        batch_m = new_mem_
                        total_loss.append(loss_)
                        print('>>> Epoch: {}, Step: {}, Loss: {:.5f}, Time: {:.2f}'.format(e, gs_, loss_,
                                                                                           time.time() - st))
                    except Exception as err:
                        print(f'Error in batch {i}, group: {j}, segments.shape: {segments.shape}, error: {err}')

                # save checkpoint every 
                if i % save_checkpoint_batch == 0:
                    print(f'>>> Saving checkpoint: {output_checkpoint_folder}/epoch-{e}_batch-{i}/model')
                    self.saver.save(self.sess, f'{output_checkpoint_folder}/epoch-{e}_batch-{i}/model')
            print(f'>>> Saving checkpoint: {output_checkpoint_folder}/epoch-{e}_batch-{i}/model')
            self.saver.save(self.sess, f'{output_checkpoint_folder}/epoch-{e}/model')

            # stop
            if stop_loss is not None and np.mean(total_loss) <= stop_loss:
                break

    def exchange_words(self, batch_x, batch_y):
        exchangeable_words_mapping = self.create_exchangeable_words_mapping()
        for i in range(len(batch_x)):
            for j in range(len(batch_x[i])):
                if batch_x[i][j] in exchangeable_words_mapping:
                    batch_x[i][j] = exchangeable_words_mapping[batch_x[i][j]]
                if batch_y[i][j] in exchangeable_words_mapping:
                    batch_y[i][j] = exchangeable_words_mapping[batch_y[i][j]]

    def create_exchangeable_words_mapping(self):
        mapping = {}

        for words in self.exchangeable_words:
            shuffled = words.copy()
            random.shuffle(shuffled)

            for i in range(len(words)):
                mapping[words[i]] = shuffled[i]

        return mapping

    ########################################
    # close
    ########################################
    def close(self):
        self.sess.close()
