"""
Simple U-Net implementation in TensorFlow

Objective: detect vehicles

y = f(X)

X: image (640, 960, 3)
y: mask (640, 960, 1)
   - binary image
   - background is masked 0
   - vehicle is masked 255

Loss function: maximize IOU

    (intersection of prediction & grount truth)
    -------------------------------
    (union of prediction & ground truth)

Notes:
    In the paper, the pixel-wise softmax was used.
    But, I used the IOU because the datasets I used are
    not labeled for segmentations

Original Paper:
    https://arxiv.org/abs/1505.04597
"""
import time
import os
import pandas as pd
import tensorflow as tf

try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError

def image_augmentation(image, mask):
    """Returns (maybe) augmented images

    (1) Random flip (left <--> right)
    (2) Random flip (up <--> down)
    (3) Random brightness
    (4) Random hue

    Args:
        image (3-D Tensor): Image tensor of (H, W, C)
        mask (3-D Tensor): Mask image tensor of (H, W, 1)

    Returns:
        image: Maybe augmented image (same shape as input `image`)
        mask: Maybe augmented mask (same shape as input `mask`)
    """
    concat_image = tf.concat([image, mask], axis=-1)

    maybe_flipped = tf.image.random_flip_left_right(concat_image)
    maybe_flipped = tf.image.random_flip_up_down(concat_image)

    image = maybe_flipped[:, :, :-1]
    mask = maybe_flipped[:, :, -1:]

    image = tf.image.random_brightness(image, 0.7)
    image = tf.image.random_hue(image, 0.3)

    return image, mask



def get_image_mask(queue, data_root = tf.Variable("", trainable=False),
                   augmentation=True,
                   img_decoder = tf.image.decode_png,
                   height=None, width=None):
    """Returns `image` and `mask`
    Input pipeline:
        Queue -> CSV -> FileRead -> Decode JPEG
    (1) Queue contains a CSV filename
    (2) Text Reader opens the CSV
        CSV file contains two columns
        ["path/to/image.jpg", "path/to/mask.jpg"]
    (3) File Reader opens both files
    (4) Decode JPEG to tensors
    Notes:
        height, width = 640, 960
    Returns
        image (3-D Tensor): (640, 960, 3)
        mask (3-D Tensor): (640, 960, 1)
    """
    text_reader = tf.TextLineReader(skip_header_lines=1)
    _, csv_content = text_reader.read(queue)

    image_path, mask_path = tf.decode_csv(csv_content, 
                                          record_defaults=[[""], [""]]
                                          )

    image_path = tf.add(data_root, image_path)
    mask_path = tf.add(data_root, mask_path)

    image_file = tf.read_file(image_path)
    mask_file = tf.read_file(mask_path)

    image = img_decoder(image_file, channels=3)
    if height is not None and width is not None:
        image.set_shape([height, width, 3])
    image = tf.cast(image, tf.float32)

    mask = img_decoder(mask_file, channels=1)
    if height is not None and width is not None:
        mask.set_shape([height, width, 1])
    mask = tf.cast(mask, tf.float32)
    #mask = mask / (tf.reduce_max(mask) + 1e-7)

    if augmentation:
        image, mask = image_augmentation(image, mask)

    return image, mask

def get_line_num(infile):
    nlines = 0
    with open(infile) as fh:
        for _ in fh:
            nlines += 1
    return nlines

def conv_conv_pool(input_, n_filters, training, name, pool=True, activation=tf.nn.relu):
    """{Conv -> BN -> RELU}x2 -> {Pool, optional}

    Args:
        input_ (4-D Tensor): (batch_size, H, W, C)
        n_filters (list): number of filters [int, int]
        training (1-D Tensor): Boolean Tensor
        name (str): name postfix
        pool (bool): If True, MaxPool2D
        activation: Activaion functions

    Returns:
        net: output of the Convolution operations
        pool (optional): output of the max pooling operations
    """
    net = input_

    with tf.variable_scope("layer{}".format(name)):
        for i, F in enumerate(n_filters):
            net = tf.layers.conv2d(net, F, (3, 3), activation=None, padding='same', name="conv_{}".format(i + 1))
            net = tf.layers.batch_normalization(net, training=training, name="bn_{}".format(i + 1))
            net = activation(net, name="relu{}_{}".format(name, i + 1))

        if pool is False:
            return net

        pool = tf.layers.max_pooling2d(net, (2, 2), strides=(2, 2), name="pool_{}".format(name))

        return net, pool


def upsample_concat(inputA, input_B, name):
    """Upsample `inputA` and concat with `input_B`

    Args:
        input_A (4-D Tensor): (N, H, W, C)
        input_B (4-D Tensor): (N, 2*H, 2*H, C2)
        name (str): name of the concat operation

    Returns:
        output (4-D Tensor): (N, 2*H, 2*W, C + C2)
    """
    upsample = upsampling_2D(inputA, size=(2, 2), name=name)

    return tf.concat([upsample, input_B], axis=-1, name="concat_{}".format(name))


def upsampling_2D(tensor, name, size=(2, 2)):
    """Upsample/Rescale `tensor` by size

    Args:
        tensor (4-D Tensor): (N, H, W, C)
        name (str): name of upsampling operations
        size (tuple, optional): (height_multiplier, width_multiplier)
            (default: (2, 2))

    Returns:
        output (4-D Tensor): (N, h_multiplier * H, w_multiplier * W, C)
    """
    H, W, _ = tensor.get_shape().as_list()[1:]

    H_multi, W_multi = size
    target_H = H * H_multi
    target_W = W * W_multi

    return tf.image.resize_nearest_neighbor(tensor, (target_H, target_W), name="upsample_{}".format(name))


def make_unet(X, training,
                activation=tf.nn.sigmoid,
                classes=1):
    """Build a U-Net architecture

    Args:
        X (4-D Tensor): (N, H, W, C)
        training (1-D Tensor): Boolean Tensor is required for batchnormalization layers

    Returns:
        output (4-D Tensor): (N, H, W, C)
            Same shape as the `input` tensor

    Notes:
        U-Net: Convolutional Networks for Biomedical Image Segmentation
        https://arxiv.org/abs/1505.04597
    """
    net = X / 127.5 - 1
    net = tf.layers.conv2d(net, 3, (1, 1), name="color_space_adjust")
    conv1, pool1 = conv_conv_pool(net, [8, 8], training, name=1)
    conv2, pool2 = conv_conv_pool(pool1, [16, 16], training, name=2)
    conv3, pool3 = conv_conv_pool(pool2, [32, 32], training, name=3)
    conv4, pool4 = conv_conv_pool(pool3, [64, 64], training, name=4)
    conv5 = conv_conv_pool(pool4, [128, 128], training, name=5, pool=False)

    up6 = upsample_concat(conv5, conv4, name=6)
    conv6 = conv_conv_pool(up6, [64, 64], training, name=6, pool=False)

    up7 = upsample_concat(conv6, conv3, name=7)
    conv7 = conv_conv_pool(up7, [32, 32], training, name=7, pool=False)

    up8 = upsample_concat(conv7, conv2, name=8)
    conv8 = conv_conv_pool(up8, [16, 16], training, name=8, pool=False)

    up9 = upsample_concat(conv8, conv1, name=9)
    conv9 = conv_conv_pool(up9, [8, 8], training, name=9, pool=False)

    return tf.layers.conv2d(conv9, classes, (1, 1), name='final',
                            activation=activation, padding='same')


def sparse_iou(y_pred, y_true):
    channels = y_pred.shape[-1]
    iou_ = []
    for cc in range(channels):
        iou_.append(  IOU_(y_pred[:,:,:,cc], tf.equal(y_true,cc)) )
    return tf.reduce_mean(iou_)

def IOU_(y_pred, y_true):
    """Returns a (approx) IOU score

    intesection = y_pred.flatten() * y_true.flatten()
    Then, IOU = 2 * intersection / (y_pred.sum() + y_true.sum() + 1e-7) + 1e-7

    Args:
        y_pred (4-D array): (N, H, W, 1)
        y_true (4-D array): (N, H, W, 1)

    Returns:
        float: IOU score
    """
    H, W = y_pred.get_shape().as_list()[1:3]

    pred_flat = tf.reshape(y_pred, [-1, H * W])
    true_flat = tf.reshape(y_true, [-1, H * W])

    true_flat = tf.cast(true_flat, tf.float32)
    intersection = 2 * tf.reduce_sum(pred_flat * true_flat, axis=1) + 1e-7
    #true_flat = tf.cast(true_flat, tf.bool)
    #intersection = 2 * tf.reduce_sum(tf.boolean_mask(pred_flat, true_flat), axis=1) + 1e-7
    denominator = tf.reduce_sum(pred_flat, axis=1) + tf.cast(tf.reduce_sum(true_flat, axis=1), tf.float32) + 1e-7

    return tf.reduce_mean(intersection / denominator)


def make_train_op(y_pred, y_true,
        learning_rate=0.001, beta1=0.9, beta2=0.999, epsilon=1e-08):
    """Returns a training operation

    Loss function = - IOU(y_pred, y_true)

    IOU is

        (the area of intersection)
        --------------------------
        (the area of two boxes)

    Args:
        y_pred (4-D Tensor): (N, H, W, 1)
        y_true (4-D Tensor): (N, H, W, 1)

    Returns:
        train_op: minimize operation
    """
    #loss = -IOU_(y_pred, y_true)
    loss = -sparse_iou(y_pred, y_true)

    global_step = tf.train.get_or_create_global_step()

    optim = tf.train.AdamOptimizer(learning_rate=learning_rate, beta1=beta1,
                                   beta2=beta2, epsilon=epsilon)
    return optim.minimize(loss, global_step=global_step)


def read_flags():
    """Returns flags"""

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root",
                        default="../data/data_256_subsample_4x/",
                        type=str,
                        help="prefix for filenames in csv files")

    parser.add_argument("--train",
                        default="./train.csv",
                        type=str,
                        help="training csv file")

    parser.add_argument("--test",
                        default="./test.csv",
                        type=str,
                        help="training csv file")

    parser.add_argument("--epochs",
                        default=8,
                        type=int,
                        help="Number of epochs (default: 8)")

    parser.add_argument("--learning-rate",
                        default=0.001,
                        type=float,
                        help="learning rate")

    parser.add_argument("--batch-size",
                        default=16,
                        type=int,
                        help="Batch size")

    parser.add_argument("-channels",
                        default=5,
                        type=int,
                        help="number of class channels")

    parser.add_argument("--height",
                        default=256,
                        type=int,
                        help="height")

    parser.add_argument("-w",
                        default=256,
                        type=int,
                        help="width")

    parser.add_argument("--logdir",
                        default="logdir",
                        help="Tensorboard log directory (default: logdir)")

    parser.add_argument("--ckdir",
                        default="models",
                        help="Checkpoint directory (default: models)")

    flags = parser.parse_args()
    return flags


def main(flags):
    n_train = get_line_num(flags.train)
    n_test = get_line_num(flags.test)

    current_time = time.strftime("%m/%d/%H/%M/%S")
    train_logdir = os.path.join(flags.logdir, "train", current_time)
    test_logdir = os.path.join(flags.logdir, "test", current_time)

    tf.reset_default_graph()
    X = tf.placeholder(tf.float32, shape=[None, flags.height, flags.w, 3], name="X")
    y = tf.placeholder(tf.float32, shape=[None, flags.height, flags.w, 1],
                       name="y")
    mode = tf.placeholder(tf.bool, name="mode")

    if flags.channels>1:
        activation = tf.nn.softmax
    else:
        activation = tf.nn.sigmoid

    pred = make_unet(X, mode,
                     activation=activation,
                     classes=flags.channels)

    tf.add_to_collection("inputs", X)
    tf.add_to_collection("inputs", mode)
    tf.add_to_collection("outputs", pred)

    tf.summary.histogram("Predicted Mask", pred)
    tf.summary.image("Predicted Mask", pred)

    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

    with tf.control_dependencies(update_ops):
        train_op = make_train_op(pred, y, learning_rate=flags.learning_rate)

    #IOU_op = IOU_(pred, y)
    IOU_op = sparse_iou(pred, y)
    IOU_op = tf.Print(IOU_op, [IOU_op, mode])
    tf.summary.scalar("IOU", IOU_op)

    data_root_ = tf.Variable(flags.data_root, trainable=False)
    train_csv = tf.train.string_input_producer([flags.train])
    test_csv = tf.train.string_input_producer([flags.test])
    train_image, train_mask = get_image_mask(train_csv, data_root = data_root_,
											height=flags.height, width=flags.w)
    test_image, test_mask = get_image_mask(test_csv, data_root = data_root_,
                                           augmentation=False,
									       height=flags.height, width=flags.w)

    X_batch_op, y_batch_op = tf.train.shuffle_batch([train_image, train_mask],
                                                    batch_size=flags.batch_size,
                                                    capacity=flags.batch_size * 5,
                                                    min_after_dequeue=flags.batch_size * 2,
                                                    allow_smaller_final_batch=True)

    X_test_op, y_test_op = tf.train.batch([test_image, test_mask],
                                          batch_size=flags.batch_size,
                                          capacity=flags.batch_size * 2,
                                          allow_smaller_final_batch=True)

    summary_op = tf.summary.merge_all()

    with tf.Session() as sess:
        train_summary_writer = tf.summary.FileWriter(train_logdir, sess.graph)
        test_summary_writer = tf.summary.FileWriter(test_logdir)

        init = tf.global_variables_initializer()
        sess.run(init)

        saver = tf.train.Saver()
        if os.path.exists(flags.ckdir) and tf.train.checkpoint_exists(flags.ckdir):
            latest_check_point = tf.train.latest_checkpoint(flags.ckdir)
            if latest_check_point is not None:
                print("restoring from\t%s" % latest_check_point)
                saver.restore(sess, latest_check_point)
        else:
            try:
                os.rmdir(flags.ckdir)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.mkdir(flags.ckdir)

        try:
            global_step = tf.train.get_global_step(sess.graph)

            coord = tf.train.Coordinator()
            threads = tf.train.start_queue_runners(coord=coord)

            for epoch in range(flags.epochs):

                for step in range(0, n_train, flags.batch_size):

                    X_batch, y_batch = sess.run([X_batch_op, y_batch_op])

                    _, step_iou, step_summary, global_step_value = sess.run(
                        [train_op, IOU_op, summary_op, global_step],
                        feed_dict={X: X_batch,
                                   y: y_batch,
                                   mode: True})

                    train_summary_writer.add_summary(step_summary, global_step_value)

                total_iou = 0
                for step in range(0, n_test, flags.batch_size):
                    X_test, y_test = sess.run([X_test_op, y_test_op])
                    step_iou, step_summary = sess.run(
                        [IOU_op, summary_op],
                        feed_dict={X: X_test,
                                   y: y_test,
                                   mode: False})

                    total_iou += step_iou * X_test.shape[0]

                    test_summary_writer.add_summary(step_summary, (epoch + 1) * (step + 1))

            saver.save(sess, "{}/model.ckpt".format(flags.ckdir))

        finally:
            coord.request_stop()
            coord.join(threads)
            saver.save(sess, "{}/model.ckpt".format(flags.ckdir))


if __name__ == '__main__':
    flags = read_flags()
    main(flags)
