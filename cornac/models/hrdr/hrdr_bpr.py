import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers, initializers, Input

from ...utils import get_rng
from ...utils.init_utils import uniform
from ..narre.narre import TextProcessor, AddGlobalBias
from ..narre.narre_bpr import get_item_review_pairs
from .hrdr import get_data


class Model:
    def __init__(self, n_users, n_items, vocab, global_mean,
                 n_factors=32, embedding_size=100, id_embedding_size=32,
                 attention_size=16, kernel_sizes=[3], n_filters=64,
                 n_user_mlp_factors=128, n_item_mlp_factors=128,
                 dropout_rate=0.5, max_text_length=50,
                 pretrained_word_embeddings=None, verbose=False, seed=None):
        self.n_users = n_users
        self.n_items = n_items
        self.n_vocab = vocab.size
        self.global_mean = global_mean
        self.n_factors = n_factors
        self.embedding_size = embedding_size
        self.id_embedding_size = id_embedding_size
        self.attention_size = attention_size
        self.kernel_sizes = kernel_sizes
        self.n_filters = n_filters
        self.n_user_mlp_factors = n_user_mlp_factors
        self.n_item_mlp_factors = n_item_mlp_factors
        self.dropout_rate = dropout_rate
        self.max_text_length = max_text_length
        self.verbose = verbose
        if seed is not None:
            self.rng = get_rng(seed)
            tf.random.set_seed(seed)

        embedding_matrix = uniform(shape=(self.n_vocab, self.embedding_size), low=-0.5, high=0.5, random_state=self.rng)
        embedding_matrix[:4, :] = np.zeros((4, self.embedding_size))
        if pretrained_word_embeddings is not None:
            oov_count = 0
            for word, idx in vocab.tok2idx.items():
                embedding_vector = pretrained_word_embeddings.get(word)
                if embedding_vector is not None:
                    embedding_matrix[idx] = embedding_vector
                else:
                    oov_count += 1
            if self.verbose:
                print("Number of OOV words: %d" % oov_count)

        embedding_matrix = initializers.Constant(embedding_matrix)
        i_user_id = Input(shape=(1,), dtype="int32", name="input_user_id")
        i_item_i_id = Input(shape=(1,), dtype="int32", name="input_item_i_id")
        i_item_j_id = Input(shape=(1,), dtype="int32", name="input_item_j_id")
        i_user_rating = Input(shape=(self.n_items), dtype="float32", name="input_user_rating")
        i_item_i_rating = Input(shape=(self.n_users), dtype="float32", name="input_item_i_rating")
        i_item_j_rating = Input(shape=(self.n_users), dtype="float32", name="input_item_j_rating")
        i_user_review = Input(shape=(None, self.max_text_length), dtype="int32", name="input_user_review")
        i_item_i_review = Input(shape=(None, self.max_text_length), dtype="int32", name="input_item_i_review")
        i_item_j_review = Input(shape=(None, self.max_text_length), dtype="int32", name="input_item_j_review")
        i_user_num_reviews = Input(shape=(1,), dtype="int32", name="input_user_number_of_review")
        i_item_i_num_reviews = Input(shape=(1,), dtype="int32", name="input_item_i_number_of_review")
        i_item_j_num_reviews = Input(shape=(1,), dtype="int32", name="input_item_j_number_of_review")

        l_user_review_embedding = layers.Embedding(self.n_vocab, self.embedding_size, embeddings_initializer=embedding_matrix, mask_zero=True, name="layer_user_review_embedding")
        l_item_review_embedding = layers.Embedding(self.n_vocab, self.embedding_size, embeddings_initializer=embedding_matrix, mask_zero=True, name="layer_item_review_embedding")
        l_user_embedding = layers.Embedding(self.n_users, self.id_embedding_size, embeddings_initializer="uniform", name="user_embedding")
        l_item_embedding = layers.Embedding(self.n_items, self.id_embedding_size, embeddings_initializer="uniform", name="item_embedding")

        user_bias = layers.Embedding(self.n_users, 1, embeddings_initializer=tf.initializers.Constant(0.1), name="user_bias")
        item_bias = layers.Embedding(self.n_items, 1, embeddings_initializer=tf.initializers.Constant(0.1), name="item_bias")

        user_text_processor = TextProcessor(self.max_text_length, filters=self.n_filters, kernel_sizes=self.kernel_sizes, dropout_rate=self.dropout_rate, name='user_text_processor')
        item_text_processor = TextProcessor(self.max_text_length, filters=self.n_filters, kernel_sizes=self.kernel_sizes, dropout_rate=self.dropout_rate, name='item_text_processor')

        user_review_h = user_text_processor(l_user_review_embedding(i_user_review), training=True)
        item_i_review_h = item_text_processor(l_item_review_embedding(i_item_i_review), training=True)
        item_j_review_h = item_text_processor(l_item_review_embedding(i_item_j_review), training=True)

        l_user_mlp = keras.models.Sequential([
            layers.Dense(self.n_user_mlp_factors, input_dim=self.n_items, activation="relu"),
            layers.Dense(self.n_user_mlp_factors // 2, activation="relu"),
            layers.Dense(self.n_filters, activation="relu"),
            layers.BatchNormalization(),
        ])
        l_item_mlp = keras.models.Sequential([
            layers.Dense(self.n_item_mlp_factors, input_dim=self.n_users, activation="relu"),
            layers.Dense(self.n_item_mlp_factors // 2, activation="relu"),
            layers.Dense(self.n_filters, activation="relu"),
            layers.BatchNormalization(),
        ])
        user_rating_h = l_user_mlp(i_user_rating)
        item_i_rating_h = l_item_mlp(i_item_i_rating)
        item_j_rating_h = l_item_mlp(i_item_j_rating)
        # mlp
        a_user = layers.Dense(1, activation=None, use_bias=True)(
            layers.Dense(self.attention_size, activation="relu", use_bias=True)(
                tf.multiply(
                    user_review_h,
                    tf.expand_dims(user_rating_h, 1)
                )
            )
        )
        a_user_masking = tf.expand_dims(tf.sequence_mask(tf.reshape(i_user_num_reviews, [-1]), maxlen=i_user_review.shape[1]), -1)
        user_attention = layers.Softmax(axis=1, name="user_attention")(a_user, a_user_masking)

        a_item_i = layers.Dense(1, activation=None, use_bias=True)(
            layers.Dense(self.attention_size, activation="relu", use_bias=True)(
                tf.multiply(
                    item_i_review_h,
                    tf.expand_dims(item_i_rating_h, 1)
                )
            )
        )
        a_item_j = layers.Dense(1, activation=None, use_bias=True)(
            layers.Dense(self.attention_size, activation="relu", use_bias=True)(
                tf.multiply(
                    item_j_review_h,
                    tf.expand_dims(item_j_rating_h, 1)
                )
            )
        )
        a_item_i_masking = tf.expand_dims(tf.sequence_mask(tf.reshape(i_item_i_num_reviews, [-1]), maxlen=i_item_i_review.shape[1]), -1)
        item_i_attention = layers.Softmax(axis=1, name="item_i_attention")(a_item_i, a_item_i_masking)
        a_item_j_masking = tf.expand_dims(tf.sequence_mask(tf.reshape(i_item_j_num_reviews, [-1]), maxlen=i_item_j_review.shape[1]), -1)
        item_j_attention = layers.Softmax(axis=1, name="item_j_attention")(a_item_j, a_item_j_masking)

        ou = layers.Dense(self.n_factors, use_bias=True, name="ou")(
            layers.Dropout(rate=self.dropout_rate)(
                tf.reduce_sum(layers.Multiply()([user_attention, user_review_h]), 1)
            )
        )
        oi = layers.Dense(self.n_factors, use_bias=True, name="oi")(
            layers.Dropout(rate=self.dropout_rate)(
                tf.reduce_sum(layers.Multiply()([item_i_attention, item_i_review_h]), 1)
            )
        )
        oj = layers.Dense(self.n_factors, use_bias=True, name="oj")(
            layers.Dropout(rate=self.dropout_rate)(
                tf.reduce_sum(layers.Multiply()([item_j_attention, item_j_review_h]), 1)
            )
        )

        pu = layers.Concatenate(axis=-1, name="pu")([
            tf.expand_dims(user_rating_h, 1),
            tf.expand_dims(ou, axis=1),
            l_user_embedding(i_user_id)
        ])

        qi = layers.Concatenate(axis=-1, name="qi")([
            tf.expand_dims(item_i_rating_h, 1),
            tf.expand_dims(oi, axis=1),
            l_item_embedding(i_item_i_id)
        ])
        qj = layers.Concatenate(axis=-1, name="qj")([
            tf.expand_dims(item_j_rating_h, 1),
            tf.expand_dims(oj, axis=1),
            l_item_embedding(i_item_j_id)
        ])

        W1 = layers.Dense(1, activation=None, use_bias=False, name="W1")
        add_global_bias = AddGlobalBias(init_value=self.global_mean, name="global_bias")
        r_i = layers.Add(name="prediction_i")([
            W1(tf.multiply(pu, qi)),
            user_bias(i_user_id),
            item_bias(i_item_i_id)
        ])
        r_i = add_global_bias(r_i)
        r_j = layers.Add(name="prediction_j")([
            W1(tf.multiply(pu, qj)),
            user_bias(i_user_id),
            item_bias(i_item_j_id)
        ])
        r_j = add_global_bias(r_j)
        x_ij = tf.reduce_mean(-tf.math.log(tf.nn.sigmoid(r_i-r_j)))

        self.graph = keras.Model(inputs=[
            i_user_id, 
            i_item_i_id, 
            i_item_j_id,
            i_user_rating, 
            i_user_review, 
            i_user_num_reviews, 
            i_item_i_rating, 
            i_item_i_review, 
            i_item_i_num_reviews,
            i_item_j_rating, 
            i_item_j_review, 
            i_item_j_num_reviews,
        ], outputs=x_ij)
        if self.verbose:
            self.graph.summary()

    def get_weights(self, train_set, batch_size=64, max_num_review=32):
        user_attention_review_pooling = keras.Model(inputs=[self.graph.get_layer('input_user_id').input, self.graph.get_layer('input_user_rating').input, self.graph.get_layer('input_user_review').input, self.graph.get_layer('input_user_number_of_review').input], outputs=self.graph.get_layer('pu').output)
        item_attention_pooling = keras.Model(inputs=[self.graph.get_layer('input_item_i_id').input, self.graph.get_layer('input_item_i_rating').input, self.graph.get_layer('input_item_i_review').input, self.graph.get_layer('input_item_i_number_of_review').input], outputs=[self.graph.get_layer('qi').output, self.graph.get_layer('item_i_attention').output])
        P = np.zeros((self.n_users, self.n_filters + self.n_factors + self.id_embedding_size), dtype=np.float32)
        Q = np.zeros((self.n_items, self.n_filters + self.n_factors + self.id_embedding_size), dtype=np.float32)
        A = np.zeros((self.n_items, max_num_review), dtype=np.float32)
        for batch_users in train_set.user_iter(batch_size):
            user_reviews, user_num_reviews, user_ratings = get_data(batch_users, train_set, self.max_text_length, by='user', max_num_review=max_num_review)
            pu = user_attention_review_pooling([batch_users, user_ratings, user_reviews, user_num_reviews], training=False)
            P[batch_users] = pu.numpy().reshape(len(batch_users), self.n_filters + self.n_factors + self.id_embedding_size)
        for batch_items in train_set.item_iter(batch_size):
            item_reviews, item_num_reviews, item_ratings = get_data(batch_items, train_set, self.max_text_length, by='item', max_num_review=max_num_review)
            qi, item_attention = item_attention_pooling([batch_items, item_ratings, item_reviews, item_num_reviews], training=False)
            Q[batch_items] = qi.numpy().reshape(len(batch_items), self.n_filters + self.n_factors + self.id_embedding_size)
            A[batch_items, :item_attention.shape[1]] = item_attention.numpy().reshape(item_attention.shape[:2])
        W1 = self.graph.get_layer('W1').get_weights()[0]
        bu = self.graph.get_layer('user_bias').get_weights()[0]
        bi = self.graph.get_layer('item_bias').get_weights()[0]
        mu = self.graph.get_layer('global_bias').get_weights()[0][0]
        return P, Q, W1, bu, bi, mu, A